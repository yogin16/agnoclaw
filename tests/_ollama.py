"""Shared Ollama availability guard for integration tests.

Integration tests that build an Ollama-backed harness must skip (not error)
when Ollama isn't usable. "Usable" means two independent things:

1. The daemon is reachable on localhost:11434.
2. The ``ollama`` binding is importable — it ships in the optional
   ``agnoclaw[local]`` extra and agno imports it lazily when the harness is
   built, so a reachable daemon without the binding still raises
   ``ModuleNotFoundError`` at build time.

Centralizing both checks here keeps the daemon URL/timeout and the binding
guard in one place instead of copy-pasted across conftest and each test file.
"""

from __future__ import annotations

OLLAMA_TAGS_URL = "http://localhost:11434/api/tags"
_HEALTH_TIMEOUT = 2.0


def ollama_available() -> bool:
    """True if the Ollama daemon is reachable AND the binding is importable."""
    try:
        import httpx
        import ollama  # noqa: F401 — binding ships in agnoclaw[local]

        return httpx.get(OLLAMA_TAGS_URL, timeout=_HEALTH_TIMEOUT).status_code == 200
    except Exception:  # pragma: no cover - environment guard
        return False
