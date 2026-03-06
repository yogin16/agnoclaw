"""Shared utilities for agnoclaw examples."""

import os
import sys


def detect_model() -> str:
    """Auto-detect available LLM provider.

    Priority: Anthropic → OpenAI → Ollama (fallback, no key needed).
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic:claude-sonnet-4-6"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai:gpt-4o-mini"
    try:
        import ollama

        ollama.list()
        return "ollama:llama3.2"
    except Exception:
        pass
    print("No LLM provider found. Either:")
    print("  1. Set ANTHROPIC_API_KEY")
    print("  2. Set OPENAI_API_KEY")
    print("  3. Start Ollama: ollama serve && ollama pull llama3.2")
    sys.exit(1)


def detect_embedder():
    """Auto-detect embedder for RAG examples.

    Priority: OpenAI → Ollama (fallback).
    """
    if os.environ.get("OPENAI_API_KEY"):
        from agno.knowledge.embedder.openai import OpenAIEmbedder

        return OpenAIEmbedder(id="text-embedding-3-small")
    try:
        import ollama

        ollama.list()
        from agno.knowledge.embedder.ollama import OllamaEmbedder

        return OllamaEmbedder(id="nomic-embed-text", dimensions=768)
    except Exception:
        pass
    print("No embedder found. Either set OPENAI_API_KEY or start Ollama.")
    sys.exit(1)
