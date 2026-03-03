"""
Example: Knowledge Folder RAG — text contracts → vector store → semantic Q&A

Pattern 1: Load documents from a folder, index in LanceDB, answer questions
via semantic search. Works standalone or as embedded library in a SaaS.

Run (local/Ollama):
    uv run --extra rag --extra local python examples/legal_rag/basic_contract_rag.py

Run (OpenAI):
    OPENAI_API_KEY=... uv run --extra rag python examples/legal_rag/basic_contract_rag.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _utils import detect_embedder, detect_model

from agno.agent import Agent
from agno.knowledge import Knowledge
from agno.vectordb.lancedb import LanceDb, SearchType

from agnoclaw import AgentHarness

# ── Auto-detect provider ─────────────────────────────────────────────────

MODEL = detect_model()
EMBEDDER = detect_embedder()
print(f"Using model: {MODEL}")
print(f"Using embedder: {EMBEDDER}")

# ── Setup: index sample contracts into LanceDB ──────────────────────────

CONTRACTS_DIR = Path(__file__).parent / "sample_contracts"
DB_PATH = "/tmp/agnoclaw_legal_rag_demo"

# Create knowledge base using Agno's native Knowledge class.
# agnoclaw provides the orchestration; Agno handles the RAG pipeline.
knowledge_base = Knowledge(
    vector_db=LanceDb(
        uri=DB_PATH,
        table_name="contracts",
        search_type=SearchType.hybrid,
        embedder=EMBEDDER,
    ),
)

# Load documents into the vector store
print("\nIndexing contracts into LanceDB...")
contract_files = sorted(CONTRACTS_DIR.glob("*.txt"))
for f in contract_files:
    knowledge_base.add_content(name=f.stem, path=str(f))
print(f"Indexed {len(contract_files)} contracts.\n")

# ── Create agent with knowledge base ────────────────────────────────────

# Option A: Using AgentHarness (full harness features: workspace, skills, memory)
agent = AgentHarness(
    name="contract-qa",
    model=MODEL,
    instructions=(
        "You are a legal contract analyst. Answer questions about the contracts "
        "in your knowledge base. Always cite the specific contract and clause "
        "when referencing terms. If you're unsure, say so."
    ),
)
# Inject Agno's knowledge base into the underlying agent
agent._agent.knowledge = knowledge_base
agent._agent.search_knowledge = True

print("=" * 60)
print("Contract Knowledge Base Q&A")
print("=" * 60)

# ── Ask questions ───────────────────────────────────────────────────────

questions = [
    "What is the liability cap in the NDA between Acme and Beta?",
    "Compare the termination clauses across all three contracts.",
    "Which contracts have arbitration clauses? What are the venues?",
    "What are the data security requirements in the SaaS agreement?",
]

for q in questions:
    print(f"\n[Q] {q}")
    print("-" * 60)
    agent.print_response(q, stream=True)
    print()


# ── Option B: Embedded library usage (no workspace, no skills) ──────────

print("\n" + "=" * 60)
print("Embedded Library Mode (minimal Agno agent, no harness)")
print("=" * 60)

# For SaaS embedding: use Agno's Agent directly with the knowledge base.
# No workspace, no skills, no memory files — just RAG.
embedded_agent = Agent(
    model=MODEL,
    knowledge=knowledge_base,
    search_knowledge=True,
    instructions=(
        "Answer questions about contracts in the knowledge base. "
        "Be precise. Cite specific clauses."
    ),
)

response = embedded_agent.run(
    "What happens to confidential information when the NDA is terminated?"
)
print(f"\n[Embedded Q&A]\n{response.content}")
