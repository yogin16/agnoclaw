"""
Example: Knowledge Folder RAG — PDF/text contracts → vector store → semantic Q&A

Pattern 1: Load documents from a folder, index in LanceDB, answer questions
via semantic search. Works standalone or as embedded library in a SaaS.

Run: uv run --extra rag python examples/legal_rag/basic_contract_rag.py
Requires: ANTHROPIC_API_KEY (or OPENAI_API_KEY for embeddings)
"""

from pathlib import Path

from agno.agent import Agent
from agno.embedder.openai import OpenAIEmbedder
from agno.knowledge.text import TextKnowledgeBase
from agno.vectordb.lancedb import LanceDb, SearchType

from agnoclaw import AgentHarness

# ── Setup: index sample contracts into LanceDB ──────────────────────────

CONTRACTS_DIR = Path(__file__).parent / "sample_contracts"
DB_PATH = "/tmp/agnoclaw_legal_rag_demo"

# Create a text knowledge base from the sample contracts folder.
# Agno's TextKnowledgeBase reads .txt files; for PDFs use PDFKnowledgeBase.
knowledge_base = TextKnowledgeBase(
    path=CONTRACTS_DIR,
    vector_db=LanceDb(
        uri=DB_PATH,
        table_name="contracts",
        search_type=SearchType.hybrid,
        embedder=OpenAIEmbedder(id="text-embedding-3-small"),
    ),
)

# Load documents into the vector store (only needed once; safe to call again)
print("Indexing contracts into LanceDB...")
knowledge_base.load(recreate=True)
print(f"Indexed {len(list(CONTRACTS_DIR.glob('*.txt')))} contracts.\n")

# ── Create agent with knowledge base ────────────────────────────────────

# Option A: Using AgentHarness (full harness features: workspace, skills, memory)
agent = AgentHarness(
    name="contract-qa",
    instructions=(
        "You are a legal contract analyst. Answer questions about the contracts "
        "in your knowledge base. Always cite the specific contract and clause "
        "when referencing terms. If you're unsure, say so."
    ),
)
# Inject knowledge base into the underlying Agno agent
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
print("Embedded Library Mode (minimal harness)")
print("=" * 60)

# For SaaS embedding: disable all agent-y features, just use RAG
embedded_agent = Agent(
    model="anthropic:claude-sonnet-4-6",
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
