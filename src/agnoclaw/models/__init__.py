"""Provider model adapters.

``materialize_model`` is the generic entry point: it resolves a
``provider:model_id`` spec through the provider's adapter into a
configured Agno Model instance (prompt caching, effort, provider betas)
and passes everything else through untouched. Harness callers state
*intent* only — all provider knowledge lives in the adapter modules, so
the harness (and its consumers) stay model-agnostic.

Adapters are imported lazily so importing this package never pulls in a
provider SDK you don't have installed.
"""

from __future__ import annotations

from typing import Any


def _anthropic_adapter(
    spec: str, *, cache_prompts: bool, effort: str | None
) -> Any:
    from .anthropic import materialize_anthropic_model

    return materialize_anthropic_model(
        spec, cache_prompts=cache_prompts, effort=effort
    )


# provider prefix → adapter callable. Add new providers here as their
# Agno model classes grow optimization levers worth configuring.
_ADAPTERS = {
    "anthropic": _anthropic_adapter,
}


def materialize_model(
    spec: Any,
    *,
    cache_prompts: bool = False,
    effort: str | None = None,
) -> Any:
    """Resolve a model spec through its provider adapter.

    * ``"provider:model_id"`` strings with a registered adapter come
      back as a configured Agno Model instance (or the original string
      when no options apply).
    * Everything else — non-string specs, strings without a provider
      prefix, unknown providers (including sentinel specs like
      ``mock`` / ``replay:<path>``) — passes through untouched.
    """
    if not isinstance(spec, str) or ":" not in spec:
        return spec
    provider = spec.split(":", 1)[0].strip().lower()
    adapter = _ADAPTERS.get(provider)
    if adapter is None:
        return spec
    return adapter(spec, cache_prompts=cache_prompts, effort=effort)
