"""
Local inference with Ollama — no API key required.

Demonstrates running agnoclaw with local models via Ollama.
Good for: development, testing, air-gapped environments, cost-free iterations.

Setup:
    1. Install Ollama: https://ollama.com
    2. Pull a model: ollama pull qwen3:0.6b
    3. Run: uv run python examples/ollama_local.py

Install ollama extras:
    uv sync --extra local

Available models (check `ollama ls`):
    qwen3:0.6b   — 522 MB, fast, good for testing
    qwen3:8b     — 5.2 GB, strong reasoning
    llama3.2     — 2 GB, solid general purpose
    gemma3:4b    — 3.3 GB, Google's model
"""

from agnoclaw import AgentHarness
from agnoclaw.tools.tasks import ProgressToolkit


# ── Model selection ───────────────────────────────────────────────────────────
# Change this to any model from `ollama ls`
MODEL = "qwen3:0.6b"   # smallest / fastest for testing
# MODEL = "qwen3:8b"   # better quality
# MODEL = "llama3.2"   # good general purpose

print(f"Using Ollama model: {MODEL}")
print("(No API key required — runs 100% locally)\n")


# ── Basic agent ───────────────────────────────────────────────────────────────

agent = AgentHarness(
    f"ollama:{MODEL}",
    name="ollama-agent",
    session_id="ollama-local-demo",
)

print("=== Basic response ===")
agent.print_response(
    "List three advantages of local LLM inference over cloud APIs.",
    stream=True,
)


# ── With a skill ─────────────────────────────────────────────────────────────

print("\n\n=== With code-review skill ===")
agent.print_response(
    "Review this Python function:\n"
    "```python\n"
    "def find_user(db, user_id):\n"
    "    query = f\"SELECT * FROM users WHERE id = {user_id}\"\n"
    "    return db.execute(query).fetchone()\n"
    "```",
    stream=True,
    skill="code-review",
)


# ── With tools (file operations) ──────────────────────────────────────────────

print("\n\n=== With file tools ===")
agent.print_response(
    "List the Python files in the current directory (use your file tools).",
    stream=True,
)


# ── ProgressToolkit (no API calls needed for the toolkit itself) ──────────────

print("\n\n=== ProgressToolkit demo ===")
import tempfile, json
from pathlib import Path

with tempfile.TemporaryDirectory() as tmpdir:
    toolkit = ProgressToolkit(project_dir=tmpdir)

    toolkit.write_features(json.dumps([
        {"id": "model-01", "description": "Ollama integration working"},
        {"id": "model-02", "description": "Tool calls work with local model"},
        {"id": "model-03", "description": "Skills work with local model"},
    ]))

    toolkit.update_feature_status("model-01", "passing")
    print(toolkit.read_features())
