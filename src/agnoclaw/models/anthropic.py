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
adds at most 2 on messages. Pre-existing ``cache_control`` markers on
message blocks count against the message budget so the request-level cap
holds even for caller-annotated content.

Cost note: conversation-prefix caching is tuned for agentic loops, where
each request is followed by more requests sharing the prefix within the
cache TTL. One-shot requests, or interactive chats whose turns are spaced
beyond the TTL, pay the ~1.25x cache-write premium without ever getting a
read — leave ``cache_prompts`` off for those workloads.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agno.models.anthropic import Claude

# Models that reject ``output_config: {"effort": ...}`` outright. Effort
# shipped with Sonnet 4.6 / Opus 4.5; every tier released before those —
# including still-served Claude 4.0/4.1 aliases and dated IDs — errors on
# it. A denylist keeps future models working by default.
_EFFORT_UNSUPPORTED_PREFIXES = (
    "claude-3-",
    "claude-haiku-4-5",
    "claude-sonnet-4-5",
    "claude-opus-4-0",
    "claude-opus-4-1",
    "claude-sonnet-4-0",
    "claude-opus-4-20250514",
    "claude-sonnet-4-20250514",
)

# Models where effort exists but only a subset of levels is accepted
# ("xhigh" arrived with Opus 4.7; "max" with the 4.6 family). Models not
# listed here and not in the denylist accept every level.
_EFFORT_PARTIAL_SUPPORT: tuple[tuple[str, frozenset[str]], ...] = (
    ("claude-opus-4-5", frozenset({"low", "medium", "high"})),
    ("claude-opus-4-6", frozenset({"low", "medium", "high", "max"})),
    ("claude-sonnet-4-6", frozenset({"low", "medium", "high", "max"})),
)


def _effective_effort(model_id: str, effort: str | None) -> str | None:
    """Return ``effort`` when the model accepts that level, else None."""
    if not effort:
        return None
    mid = model_id.strip().lower()
    if mid.startswith(_EFFORT_UNSUPPORTED_PREFIXES):
        return None
    for prefix, levels in _EFFORT_PARTIAL_SUPPORT:
        if mid.startswith(prefix):
            return effort if effort in levels else None
    return effort


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
    (or effort levels) the API rejects. When no option applies, the spec
    string is returned unchanged so Agno's normal string resolution takes
    over.

    A cache-enabled instance returned here should be owned by exactly one
    ``AgentHarness`` — the harness installs its own runtime system block
    on the instance, so sharing one across harnesses leaks one harness's
    session metadata into the other's requests.
    """
    model_id = spec.split(":", 1)[1].strip()
    effective_effort = _effective_effort(model_id, effort)
    if not cache_prompts and effective_effort is None:
        return spec
    kwargs: dict[str, Any] = {"id": model_id}
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


def _block_is_annotatable(block: dict[str, Any]) -> bool:
    block_type = block.get("type")
    if block_type in _INELIGIBLE_BLOCK_TYPES:
        return False
    # The API rejects empty text blocks as cache targets.
    if block_type == "text" and not (block.get("text") or "").strip():
        return False
    return True


def annotate_conversation_breakpoints(
    messages: Any,
    *,
    block_window: int = _BLOCK_WINDOW,
    max_message_breakpoints: int = 2,
) -> None:
    """Tag Anthropic-format ``messages`` with cache breakpoints.

    Walking from the end of the conversation backwards:

    * the last eligible content block always gets ``cache_control`` —
      that is the breakpoint the *next* request's prefix lookup hits;
    * after each breakpoint, another is placed once more than
      ``block_window`` blocks have been walked without one, keeping every
      gap inside Anthropic's 20-block lookback window;
    * blocks already carrying ``cache_control`` count toward
      ``max_message_breakpoints`` and act as breakpoints for the window
      walk, so the per-request marker total stays bounded even when the
      caller annotated content themselves;
    * string message content is promoted to a single text block when it
      needs the annotation; thinking blocks and empty text blocks are
      never annotated (the API rejects both); non-dict blocks (SDK
      objects in assistant turns) can't carry the annotation but still
      count toward the lookback window.

    Only the per-request message dicts are modified. Content lists and
    block dicts are never mutated — Agno passes some of them by reference
    from its stored session messages, and an in-place ``cache_control``
    write there would persist across requests, accumulate markers past
    Anthropic's 4-breakpoint cap, and pollute stored session state.
    Blocks that need an annotation are replaced by copies inside a fresh
    content list.

    A no-op on empty or non-list input.
    """
    if not isinstance(messages, list) or not messages:
        return
    # Pre-scan: every marker already present counts against the budget,
    # wherever it sits — otherwise our additions could push the request
    # past Anthropic's 4-breakpoint cap.
    placed = sum(
        1
        for message in messages
        if isinstance(message, dict) and isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and "cache_control" in block
    )
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
            blocks_since_breakpoint += 1
            if blocks_since_breakpoint > block_window:
                want_breakpoint = True
            if not isinstance(block, dict):
                continue
            if "cache_control" in block:
                # Pre-existing marker (already counted by the pre-scan):
                # a valid lookback anchor — reset the window around it.
                want_breakpoint = False
                blocks_since_breakpoint = 0
                continue
            if not want_breakpoint:
                continue
            if not _block_is_annotatable(block):
                continue
            annotated = dict(block)
            annotated["cache_control"] = dict(_CACHE_CONTROL)
            fresh_content = list(content)
            fresh_content[b_idx] = annotated
            message["content"] = fresh_content
            content = fresh_content
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

    A cache-enabled instance should be owned by exactly one
    ``AgentHarness``: the harness installs its runtime system block via
    ``system_prompt_blocks``, and sharing one instance across harnesses
    would serve the last harness's session metadata to all of them.
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
