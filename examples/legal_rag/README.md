# Legal Contract RAG Examples

Three RAG patterns demonstrating agnoclaw as an embedded agent harness for legal contract analysis:

## RAG Patterns

### 1. Knowledge Folder RAG (`basic_contract_rag.py`)
Load PDFs/DOCX from a folder → vector store → semantic search → answer questions.
Uses Agno's `PDFKnowledgeBase` + `LanceDb` + `OpenAIEmbedder`.

### 2. Data Agent RAG (`data_agent_rag.py`)
SQL database with contract metadata → natural language to SQL → deep research with queries.
Uses Agno's `SQLTools` + custom schema-aware agent.

### 3. Hybrid RAG (`hybrid_contract_rag.py`)
Combines DB records (metadata) + file-based PDFs (full text). The agent queries the
DB for contract records, resolves media URLs/file paths, extracts PDF content, and
cross-references structured data with unstructured text.

## Setup

```bash
# Install RAG dependencies
pip install agnoclaw[rag]

# Or with uv
uv sync --extra rag

# Required env vars
export ANTHROPIC_API_KEY=sk-ant-...    # or OPENAI_API_KEY for embeddings
```

## Files

| File | Pattern | Description |
|------|---------|-------------|
| `basic_contract_rag.py` | Knowledge Folder | PDF → LanceDB → semantic Q&A |
| `clause_aware_chunking.py` | Custom Chunker | Split on clause/section boundaries |
| `custom_toolkit.py` | Toolkit Wrapper | `ContractToolkit` for AgentHarness |
| `data_agent_rag.py` | Data Agent | SQL + schemas → NL queries |
| `hybrid_contract_rag.py` | Hybrid | DB records + PDF files combined |
| `risk_assessment.py` | Structured Output | Pydantic models for risk scoring |
| `contract_analysis_team.py` | Multi-Agent | Extractor → Analyst → Compliance → Writer |
| `sample_contracts/` | Test Data | NDA, MSA, Service Agreement (plain text) |

## Embedded Library Usage

All examples work both standalone and as embedded library components:

```python
# Standalone
agent = AgentHarness(tools=[ContractToolkit(knowledge_base)])
agent.print_response("What are the termination clauses in the NDA?")

# Embedded in a SaaS
from agnoclaw import AgentHarness
from agnoclaw.tools import get_default_tools

class ContractService:
    def __init__(self, db_url, docs_path):
        self.agent = AgentHarness(
            tools=[ContractToolkit(db_url, docs_path)],
            config=HarnessConfig(enable_bash=False, enable_web_search=False),
        )

    def analyze(self, question: str, user_id: str) -> str:
        return self.agent.run(question, user_id=user_id).content
```
