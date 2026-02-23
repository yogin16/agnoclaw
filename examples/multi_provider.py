"""
Example: Multi-Provider Model Comparison

Demonstrates:
- Running the same task on multiple providers
- Provider-specific model IDs
- Comparing outputs across Claude, OpenAI, Gemini, Ollama
- Using Groq for fast/cheap inference
"""

from agnoclaw import HarnessAgent

TASK = "Explain the CAP theorem in 3 bullet points for a senior engineer."


# ── Anthropic (Claude) ────────────────────────────────────────────────────

claude_agent = HarnessAgent(
    model_id="claude-sonnet-4-6",
    provider="anthropic",
    name="claude",
)

print("=== Claude (Anthropic) ===")
claude_result = claude_agent.run(TASK)
print(claude_result.content)


# ── OpenAI (GPT) ──────────────────────────────────────────────────────────
# Requires OPENAI_API_KEY env var

try:
    gpt_agent = HarnessAgent(
        model_id="gpt-4o",
        provider="openai",
        name="gpt4o",
    )
    print("\n=== GPT-4o (OpenAI) ===")
    gpt_result = gpt_agent.run(TASK)
    print(gpt_result.content)
except Exception as e:
    print(f"\n=== GPT-4o skipped: {e} ===")


# ── Google (Gemini) ───────────────────────────────────────────────────────
# Requires GOOGLE_API_KEY env var

try:
    gemini_agent = HarnessAgent(
        model_id="gemini-2.0-flash",
        provider="google",
        name="gemini",
    )
    print("\n=== Gemini Flash (Google) ===")
    gemini_result = gemini_agent.run(TASK)
    print(gemini_result.content)
except Exception as e:
    print(f"\n=== Gemini skipped: {e} ===")


# ── Groq (fast + cheap) ───────────────────────────────────────────────────
# Requires GROQ_API_KEY env var

try:
    groq_agent = HarnessAgent(
        model_id="llama-3.3-70b-versatile",
        provider="groq",
        name="groq-llama",
    )
    print("\n=== Llama 3.3 70B (Groq) ===")
    groq_result = groq_agent.run(TASK)
    print(groq_result.content)
except Exception as e:
    print(f"\n=== Groq skipped: {e} ===")


# ── Ollama (local) ────────────────────────────────────────────────────────
# Requires Ollama running locally: `ollama serve`

try:
    ollama_agent = HarnessAgent(
        model_id="llama3.2",
        provider="ollama",
        name="ollama-local",
    )
    print("\n=== Llama 3.2 (Ollama local) ===")
    ollama_result = ollama_agent.run(TASK)
    print(ollama_result.content)
except Exception as e:
    print(f"\n=== Ollama skipped: {e} ===")


# ── Env-based provider selection ─────────────────────────────────────────
# Production pattern: set AGNOCLAW_DEFAULT_MODEL and AGNOCLAW_DEFAULT_PROVIDER
# in the environment or ~/.agnoclaw/config.toml

env_agent = HarnessAgent()  # picks up defaults from env/config
print(f"\n=== Default from config: model={env_agent._model} ===")
