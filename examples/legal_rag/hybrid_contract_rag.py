"""
Example: Hybrid RAG — DB records + PDF files combined

Pattern 3: The agent queries a SQL database for contract metadata, resolves
file paths or media URLs to actual contract PDFs/text files, extracts content,
and cross-references structured data (metadata, parties, clauses) with
unstructured text (full contract documents).

This is the most realistic enterprise pattern — the database holds contract
records with metadata, and each record points to the actual contract document
(either a local file path or a remote URL).

Run: uv run python examples/legal_rag/hybrid_contract_rag.py

The example auto-detects available providers (Anthropic → OpenAI → Ollama).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _utils import detect_model

from agno.tools.toolkit import Toolkit

from agnoclaw import AgentHarness


class HybridContractToolkit(Toolkit):
    """
    Hybrid toolkit: SQL metadata + file-based document access.

    Combines structured queries (parties, dates, values, risk levels)
    with unstructured document retrieval (full contract text from files).

    This is the recommended pattern for SaaS embedding where:
      - Contracts are stored in a database (metadata, parties, terms)
      - Actual contract documents are PDFs/files referenced by the DB record
      - The agent needs to cross-reference both sources
    """

    def __init__(self, db_path: str, docs_base_path: str | Path = ""):
        super().__init__(name="hybrid_contracts")
        self._db_path = db_path
        self._docs_base = Path(docs_base_path).resolve() if docs_base_path else Path.cwd()

        self.register(self.get_schema)
        self.register(self.query_contracts)
        self.register(self.get_contract_record)
        self.register(self.get_contract_document)
        self.register(self.cross_reference)
        self.register(self.get_risk_dashboard)

    def get_schema(self) -> str:
        """Get the database schema for contract tables."""
        return """
Database Schema:

TABLE contracts:
  id, name, contract_type, status, effective_date, expiration_date,
  governing_law, total_value, currency, media_url, file_path

TABLE parties:
  id, contract_id, party_name, party_role, entity_type, jurisdiction

TABLE clauses:
  id, contract_id, clause_type, section_number, summary, risk_level,
  liability_cap, notes

Key relationships:
  - parties.contract_id → contracts.id
  - clauses.contract_id → contracts.id
  - contracts.file_path → path to actual contract document
  - contracts.media_url → URL to contract PDF (alternative to file_path)
"""

    def query_contracts(self, sql: str) -> str:
        """
        Execute a SELECT query against the contracts database.

        Args:
            sql: SQL SELECT query.

        Returns:
            Query results as formatted text.
        """
        if not sql.strip().upper().startswith("SELECT"):
            return "[error] Only SELECT queries are allowed."

        try:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql).fetchall()
            conn.close()

            if not rows:
                return "No results."

            columns = rows[0].keys()
            lines = [" | ".join(columns), "-" * 60]
            for row in rows:
                lines.append(" | ".join(str(row[col]) for col in columns))
            return "\n".join(lines)
        except Exception as e:
            return f"[error] Query failed: {e}"

    def get_contract_record(self, contract_id: int) -> str:
        """
        Get the full database record for a contract including parties and clauses.

        Args:
            contract_id: Contract ID from the database.

        Returns:
            Structured metadata from the database.
        """
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row

        contract = conn.execute(
            "SELECT * FROM contracts WHERE id = ?", (contract_id,)
        ).fetchone()

        if not contract:
            conn.close()
            return f"[error] Contract {contract_id} not found."

        parties = conn.execute(
            "SELECT * FROM parties WHERE contract_id = ?", (contract_id,)
        ).fetchall()

        clauses = conn.execute(
            "SELECT * FROM clauses WHERE contract_id = ?", (contract_id,)
        ).fetchall()

        conn.close()

        parts = [
            f"=== Contract Record (ID: {contract_id}) ===",
            f"Name: {contract['name']}",
            f"Type: {contract['contract_type']}",
            f"Status: {contract['status']}",
            f"Dates: {contract['effective_date']} to {contract['expiration_date']}",
            f"Governing Law: {contract['governing_law']}",
            f"Value: {contract['currency']} {contract['total_value']:,.0f}",
            f"File: {contract['file_path'] or 'none'}",
            f"URL: {contract['media_url'] or 'none'}",
            "",
            "Parties:",
        ]
        for p in parties:
            parts.append(f"  - {p['party_name']} ({p['party_role']})")

        parts.append("\nClauses from DB:")
        for c in clauses:
            parts.append(f"  [{c['risk_level']}] §{c['section_number']} {c['clause_type']}: {c['summary']}")

        return "\n".join(parts)

    def get_contract_document(self, contract_id: int) -> str:
        """
        Retrieve the actual contract document text by resolving the file_path
        or media_url from the database record.

        This bridges the structured (DB) and unstructured (document) worlds.

        Args:
            contract_id: Contract ID from the database.

        Returns:
            Full contract document text.
        """
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        contract = conn.execute(
            "SELECT file_path, media_url, name FROM contracts WHERE id = ?",
            (contract_id,),
        ).fetchone()
        conn.close()

        if not contract:
            return f"[error] Contract {contract_id} not found."

        # Try file_path first
        if contract["file_path"]:
            file_path = self._docs_base / contract["file_path"]
            if file_path.exists():
                text = file_path.read_text(encoding="utf-8")
                return f"=== Document: {contract['name']} ===\n(Source: {file_path})\n\n{text}"
            else:
                return f"[error] File not found: {file_path}"

        # Try media_url
        if contract["media_url"]:
            return (
                f"[info] Contract '{contract['name']}' has a media URL: {contract['media_url']}\n"
                "Use web_fetch or the media toolkit to download and read the PDF."
            )

        return f"[error] No file_path or media_url for contract {contract_id}."

    def cross_reference(self, contract_id: int, question: str) -> str:
        """
        Cross-reference database metadata with the actual document text.

        Gets both the DB record (structured) and the document text (unstructured)
        so you can verify clauses, find discrepancies, or answer detailed questions.

        Args:
            contract_id: Contract ID.
            question: What to look for in the cross-reference.

        Returns:
            Both the DB record and relevant document excerpts.
        """
        record = self.get_contract_record(contract_id)
        document = self.get_contract_document(contract_id)

        return (
            f"=== Cross-Reference for Contract {contract_id} ===\n"
            f"Question: {question}\n\n"
            f"--- Database Record ---\n{record}\n\n"
            f"--- Full Document ---\n{document[:5000]}"
            f"{'... [truncated]' if len(document) > 5000 else ''}"
        )

    def get_risk_dashboard(self) -> str:
        """
        Generate a risk dashboard across all contracts.

        Combines DB metadata for a portfolio-level view.
        """
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row

        stats = conn.execute("""
            SELECT
                c.name,
                c.contract_type,
                c.total_value,
                c.expiration_date,
                COUNT(cl.id) as total_clauses,
                SUM(CASE WHEN cl.risk_level = 'critical' THEN 1 ELSE 0 END) as critical,
                SUM(CASE WHEN cl.risk_level = 'high' THEN 1 ELSE 0 END) as high,
                MAX(cl.liability_cap) as max_liability
            FROM contracts c
            LEFT JOIN clauses cl ON c.id = cl.contract_id
            GROUP BY c.id
            ORDER BY critical DESC, high DESC
        """).fetchall()
        conn.close()

        parts = ["=== Contract Risk Dashboard ===\n"]
        for s in stats:
            parts.append(
                f"  {s['name']} ({s['contract_type']})\n"
                f"    Value: ${s['total_value']:,.0f} | Expires: {s['expiration_date']}\n"
                f"    Clauses: {s['total_clauses']} total, "
                f"{s['critical']} critical, {s['high']} high\n"
                f"    Max liability: ${s['max_liability']:,.0f}" if s['max_liability'] else
                f"  {s['name']} ({s['contract_type']})\n"
                f"    Value: ${s['total_value']:,.0f} | Expires: {s['expiration_date']}\n"
                f"    Clauses: {s['total_clauses']} total, "
                f"{s['critical']} critical, {s['high']} high"
            )

        return "\n".join(parts)


# ── Demo ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    model = detect_model()
    print(f"Using model: {model}")

    # Setup database (reuse from data_agent_rag.py)
    from data_agent_rag import setup_database, DB_PATH

    setup_database()

    docs_base = Path(__file__).parent
    toolkit = HybridContractToolkit(DB_PATH, docs_base_path=docs_base)

    agent = AgentHarness(
        name="hybrid-contract-agent",
        model=model,
        tools=[toolkit],
        instructions=(
            "You are a contract analysis agent with access to both a contracts database "
            "and the actual contract documents. Use the hybrid_contracts toolkit to:\n"
            "1. Query the database for structured metadata (parties, dates, risk levels)\n"
            "2. Retrieve actual document text when you need the full contract language\n"
            "3. Cross-reference database records with document text for verification\n"
            "Always cite both your data source (DB or document) in answers."
        ),
    )

    print("=" * 60)
    print("Hybrid RAG Demo (DB + Documents)")
    print("=" * 60)

    questions = [
        "Show me the risk dashboard for our contract portfolio.",
        "Get the full NDA document and verify the liability cap matches the database record.",
        "What are the actual termination clauses in the MSA? Compare with the DB summary.",
        "Cross-reference the SaaS agreement — do the SLA terms in the document match the DB?",
    ]

    for q in questions:
        print(f"\n[Q] {q}")
        print("-" * 60)
        agent.print_response(q, stream=True)
        print()
