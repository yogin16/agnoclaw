"""
Example: Multi-Provider Model Comparison

Demonstrates:
- Running the same task on multiple providers
- New "provider:model_id" string format (simplest API)
- Legacy separate model_id + provider params (still works)
- Comparing outputs across Claude, OpenAI, Gemini, Ollama
- Using Groq for fast/cheap inference
"""

from _utils import detect_model
from agnoclaw import AgentHarness

TASK = "Explain the CAP theorem in 3 bullet points for a senior engineer."

MODEL = detect_model()

# ── Default provider (auto-detected) ─────────────────────────────────────

default_agent = AgentHarness(MODEL, name="default")

print(f"=== Default ({MODEL}) ===")
default_result = default_agent.run(TASK)
print(default_result.content)


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


# ── Env-based provider selection ─────────────────────────────────────────
# Production pattern: set AGNOCLAW_DEFAULT_MODEL and AGNOCLAW_DEFAULT_PROVIDER
# in the environment or ~/.agnoclaw/config.toml, or use detect_model()

env_agent = AgentHarness(model=MODEL)
print(f"\n=== Default from config: model={env_agent._model} ===")
