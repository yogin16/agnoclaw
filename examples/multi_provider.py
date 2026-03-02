"""
Example: Multi-Provider Model Comparison

Demonstrates:
- Running the same task on multiple providers
- New "provider:model_id" string format (simplest API)
- Legacy separate model_id + provider params (still works)
- Comparing outputs across Claude, OpenAI, Gemini, Ollama
- Using Groq for fast/cheap inference
"""

from agnoclaw import AgentHarness

TASK = "Explain the CAP theorem in 3 bullet points for a senior engineer."


# ── New API: provider:model_id as a single string ─────────────────────────

claude_agent = AgentHarness("anthropic:claude-sonnet-4-6", name="claude")

print("=== Claude (Anthropic) ===")
claude_result = claude_agent.run(TASK)
print(claude_result.content)


# ── OpenAI (GPT) ──────────────────────────────────────────────────────────
# Requires OPENAI_API_KEY env var

try:
    gpt_agent = AgentHarness("openai:gpt-4o", name="gpt4o")
    print("\n=== GPT-4o (OpenAI) ===")
    gpt_result = gpt_agent.run(TASK)
    print(gpt_result.content)
except Exception as e:
    print(f"\n=== GPT-4o skipped: {e} ===")


# ── Google (Gemini) ───────────────────────────────────────────────────────
# Requires GOOGLE_API_KEY env var

try:
    gemini_agent = AgentHarness("google:gemini-2.0-flash", name="gemini")
    print("\n=== Gemini Flash (Google) ===")
    gemini_result = gemini_agent.run(TASK)
    print(gemini_result.content)
except Exception as e:
    print(f"\n=== Gemini skipped: {e} ===")


# ── Groq (fast + cheap) ───────────────────────────────────────────────────
# Requires GROQ_API_KEY env var

try:
    groq_agent = AgentHarness("groq:llama-3.3-70b-versatile", name="groq-llama")
    print("\n=== Llama 3.3 70B (Groq) ===")
    groq_result = groq_agent.run(TASK)
    print(groq_result.content)
except Exception as e:
    print(f"\n=== Groq skipped: {e} ===")


# ── Ollama (local, no API key) ────────────────────────────────────────────
# Requires Ollama running: `ollama serve && ollama pull llama3.2`

try:
    ollama_agent = AgentHarness("ollama:llama3.2", name="ollama-local")
    print("\n=== Llama 3.2 (Ollama local) ===")
    ollama_result = ollama_agent.run(TASK)
    print(ollama_result.content)
except Exception as e:
    print(f"\n=== Ollama skipped: {e} ===")


# ── Legacy API: separate model_id + provider (deprecated, emits warning) ──

legacy_agent = AgentHarness(model_id="claude-sonnet-4-6", provider="anthropic")

# ── Env-based provider selection ─────────────────────────────────────────
# Production pattern: set AGNOCLAW_DEFAULT_MODEL and AGNOCLAW_DEFAULT_PROVIDER
# in the environment or ~/.agnoclaw/config.toml

env_agent = AgentHarness()  # picks up defaults from env/config
print(f"\n=== Default from config: model={env_agent._model} ===")
