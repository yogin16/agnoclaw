"""Anthropic ``Claude`` wrapper with conversation-prefix prompt caching.

Agno's ``Claude`` already supports caching the tools array
(``cache_tools``) and the system prompt (``cache_system_prompt``), but it
places no ``cache_control`` breakpoint in ``messages`` — so in an agentic
loop every request re-pays the whole conversation prefix (attached
documents + accumulated tool results) at full input price. On long
tool-calling runs that prefix dwarfs the system prompt.

``CacheAwareClaude`` closes that gap: before each request it tags the last
content block of the final message with ``cache_control`` (and, when a lot
of blocks trail the previous breakpoint, one intermediate block — see
``annotate_conversation_breakpoints``). Successive requests in the loop
then read the shared prefix from cache at ~0.1x input price instead of
re-processing it.

Breakpoint budget per request (Anthropic allows 4):
``cache_tools`` uses 1, ``cache_system_prompt`` uses 1, and this module
adds at most 2 on messages.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agno.models.anthropic import Claude

# Legacy models that reject ``output_config: {"effort": ...}`` — effort
# is GA on Sonnet 4.6 / Opus 4.5 and everything after; only these older
# tiers error on it. A denylist keeps future models working by default.
_EFFORT_UNSUPPORTED_PREFIXES = (
    "claude-haiku-4-5",
    "claude-sonnet-4-5",
    "claude-3-",
)

# Per-model beta headers worth auto-applying. Token-efficient tool use
# is Claude 3.7 Sonnet only per Anthropic's docs — newer models silently
# ignore the header but lose prompt-cache hits over it, so gate tightly.
_TOKEN_EFFICIENT_TOOLS_BETA = "token-efficient-tools-2025-02-19"


def _supports_effort(model_id: str) -> bool:
    return not model_id.strip().lower().startswith(_EFFORT_UNSUPPORTED_PREFIXES)


def _resolve_betas(model_id: str) -> list[str]:
    if model_id.strip().lower().startswith("claude-3-7-sonnet"):
        return [_TOKEN_EFFICIENT_TOOLS_BETA]
    return []


def materialize_anthropic_model(
    spec: str,
    *,
    cache_prompts: bool = False,
    effort: str | None = None,
) -> Claude | str:
    """Build a configured Claude instance from an ``anthropic:<id>`` spec.

    ``cache_prompts=True`` enables the full caching stack: system prompt
    + tools (Agno's native ``cache_system_prompt`` / ``cache_tools``
    flags) and the conversation prefix (``CacheAwareClaude``). ``effort``
    maps to ``output_config.effort`` and is silently dropped on models
    that reject it. When no option applies, the spec string is returned
    unchanged so Agno's normal string resolution takes over.
    """
    model_id = spec.split(":", 1)[1].strip()
    betas = _resolve_betas(model_id)
    effective_effort = effort if (effort and _supports_effort(model_id)) else None
    if not cache_prompts and not betas and effective_effort is None:
        return spec
    kwargs: dict[str, Any] = {"id": model_id}
    if betas:
        kwargs["betas"] = betas
    if cache_prompts:
        kwargs["cache_system_prompt"] = True
        kwargs["cache_tools"] = True
    if effective_effort:
        kwargs["output_config"] = {"effort": effective_effort}
    if cache_prompts:
        return CacheAwareClaude(**kwargs)
    return Claude(**kwargs)


_CACHE_CONTROL: dict[str, str] = {"type": "ephemeral"}

# Anthropic rejects ``cache_control`` on thinking blocks.
_INELIGIBLE_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking"})

# Anthropic's cache lookup walks back at most 20 content blocks from a
# breakpoint. Once more than this many blocks trail the previous
# breakpoint we drop an intermediate one, so a long agentic turn (many
# tool_use / tool_result pairs) still lands inside the lookback window
# of the next request's trailing breakpoint.
_BLOCK_WINDOW = 15


def annotate_conversation_breakpoints(
    messages: Any,
    *,
    block_window: int = _BLOCK_WINDOW,
    max_message_breakpoints: int = 2,
) -> None:
    """Tag Anthropic-format ``messages`` with cache breakpoints, in place.

    Walking from the end of the conversation backwards:

    * the last eligible content block always gets ``cache_control`` —
      that is the breakpoint the *next* request's prefix lookup hits;
    * after each placed breakpoint, another is placed once more than
      ``block_window`` blocks have been walked without one, keeping every
      gap inside Anthropic's 20-block lookback window;
    * at most ``max_message_breakpoints`` are placed in total;
    * string message content is promoted to a single text block when it
      needs the annotation; thinking blocks are skipped (the API rejects
      ``cache_control`` on them); blocks already carrying a
      ``cache_control`` are left untouched.

    A no-op on empty or non-list input.
    """
    if not isinstance(messages, list) or not messages:
        return
    placed = 0
    blocks_since_breakpoint = 0
    want_breakpoint = True  # the final eligible block always gets one
    for m_idx in range(len(messages) - 1, -1, -1):
        if placed >= max_message_breakpoints:
            return
        message = messages[m_idx]
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            if not content.strip():
                continue
            content = [{"type": "text", "text": content}]
            message["content"] = content
        if not isinstance(content, list):
            continue
        for b_idx in range(len(content) - 1, -1, -1):
            block = content[b_idx]
            if not isinstance(block, dict):
                continue
            blocks_since_breakpoint += 1
            if blocks_since_breakpoint > block_window:
                want_breakpoint = True
            if not want_breakpoint:
                continue
            if block.get("type") in _INELIGIBLE_BLOCK_TYPES:
                continue
            if "cache_control" not in block:
                block["cache_control"] = dict(_CACHE_CONTROL)
            placed += 1
            want_breakpoint = False
            blocks_since_breakpoint = 0
            if placed >= max_message_breakpoints:
                return


class _CachingMessagesProxy:
    """Wraps a ``client.messages`` (or ``client.beta.messages``) resource,
    annotating the ``messages`` kwarg before ``create`` / ``stream``."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def create(self, *args: Any, **kwargs: Any) -> Any:
        annotate_conversation_breakpoints(kwargs.get("messages"))
        return self._inner.create(*args, **kwargs)

    def stream(self, *args: Any, **kwargs: Any) -> Any:
        annotate_conversation_breakpoints(kwargs.get("messages"))
        return self._inner.stream(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _CachingBetaProxy:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    @property
    def messages(self) -> _CachingMessagesProxy:
        return _CachingMessagesProxy(self._inner.messages)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _CachingClientProxy:
    """Thin pass-through client wrapper; only the messages create/stream
    call paths are intercepted (sync and beta alike)."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    @property
    def messages(self) -> _CachingMessagesProxy:
        return _CachingMessagesProxy(self._inner.messages)

    @property
    def beta(self) -> _CachingBetaProxy:
        return _CachingBetaProxy(self._inner.beta)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


@dataclass
class CacheAwareClaude(Claude):
    """``Claude`` that also caches the conversation prefix.

    All four invoke paths (sync/async x create/stream, plus their beta
    variants) build ``messages`` internally and pass them as a kwarg to
    the Anthropic client — so the single reliable interception point is
    the client itself. ``get_client`` / ``get_async_client`` return a
    proxy that annotates cache breakpoints on the way out.

    Set ``cache_conversation=False`` to fall back to plain ``Claude``
    behavior (system/tools caching only, per the inherited flags).
    """

    cache_conversation: bool = True

    def get_client(self) -> Any:  # type: ignore[override]
        client = super().get_client()
        if not self.cache_conversation:
            return client
        return _CachingClientProxy(client)

    def get_async_client(self) -> Any:  # type: ignore[override]
        client = super().get_async_client()
        if not self.cache_conversation:
            return client
        return _CachingClientProxy(client)
