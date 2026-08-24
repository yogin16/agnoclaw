"""Lifecycle adapter for provider transports owned by agnoclaw."""

from __future__ import annotations

import inspect
import threading
from typing import Any


class OwnedAgnoModelResource:
    """Close transports created from an agnoclaw-owned Agno model specification.

    Agno models do not share one lifecycle interface. Most provider clients expose
    ``close``/``aclose`` directly; Ollama's client currently owns a private HTTPX
    transport without forwarding either method. The latter fallback is deliberately
    provider-specific and applies only to a model materialized from a string owned by
    agnoclaw. Caller-injected model objects must never be registered here.
    """

    def __init__(self, model: Any) -> None:
        self._model = model
        self._closed = False
        self._lock = threading.Lock()

    def _claim_close(self) -> bool:
        with self._lock:
            if self._closed:
                return False
            self._closed = True
            return True

    def _candidates(self) -> tuple[Any, ...]:
        if callable(getattr(self._model, "close", None)) or callable(
            getattr(self._model, "aclose", None)
        ):
            return (self._model,)
        candidates: list[Any] = []
        for name in ("client", "async_client"):
            client = getattr(self._model, name, None)
            if client is None:
                continue
            if not callable(getattr(client, "close", None)) and not callable(
                getattr(client, "aclose", None)
            ):
                module = type(client).__module__
                nested = getattr(client, "_client", None)
                if module.startswith("ollama.") and nested is not None:
                    client = nested
            if all(id(client) != id(existing) for existing in candidates):
                candidates.append(client)
        return tuple(candidates)

    def close(self) -> None:
        if not self._claim_close():
            return
        for candidate in self._candidates():
            closer = getattr(candidate, "close", None)
            if callable(closer):
                closer()

    async def aclose(self) -> None:
        if not self._claim_close():
            return
        for candidate in self._candidates():
            async_closer = getattr(candidate, "aclose", None)
            if callable(async_closer):
                result = async_closer()
                if inspect.isawaitable(result):
                    await result
                continue
            closer = getattr(candidate, "close", None)
            if callable(closer):
                closer()


__all__ = ["OwnedAgnoModelResource"]
