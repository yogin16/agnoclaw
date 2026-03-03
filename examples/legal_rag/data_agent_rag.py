"""
Example: Data Agent RAG — SQL database with contract metadata → NL queries

Pattern 2: The agent queries a SQL database containing contract metadata,
generates SQL from natural language, and performs deep research using
structured queries. Works as embedded library in a SaaS backend.

Run: uv run python examples/legal_rag/data_agent_rag.py
Requires: ANTHROPIC_API_KEY
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from agno.tools.toolkit import Toolkit

from agnoclaw import AgentHarness


# ── Setup: create and populate sample database ──────────────────────────

DB_PATH = "/tmp/agnoclaw_contracts.db"


def setup_database():
    """Create a sample contracts database with realistic metadata."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("DROP TABLE IF EXISTS contracts")
    cursor.execute("DROP TABLE IF EXISTS parties")
    cursor.execute("DROP TABLE IF EXISTS clauses")

    cursor.execute("""
        CREATE TABLE contracts (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            contract_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            effective_date TEXT,
            expiration_date TEXT,
            governing_law TEXT,
            total_value REAL,
            currency TEXT DEFAULT 'USD',
            media_url TEXT,
            file_path TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE parties (
            id INTEGER PRIMARY KEY,
            contract_id INTEGER REFERENCES contracts(id),
            party_name TEXT NOT NULL,
            party_role TEXT NOT NULL,
            entity_type TEXT,
            jurisdiction TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE clauses (
            id INTEGER PRIMARY KEY,
            contract_id INTEGER REFERENCES contracts(id),
            clause_type TEXT NOT NULL,
            section_number TEXT,
            summary TEXT,
            risk_level TEXT DEFAULT 'low',
            liability_cap REAL,
            notes TEXT
        )
    """)

    # Insert sample data
    contracts = [
        (1, "Acme-Beta NDA", "NDA", "active", "2026-01-15", "2029-01-15",
         "California", 0, "USD", None, "sample_contracts/nda.txt"),
        (2, "TechServ-GlobalCorp MSA", "MSA", "active", "2026-02-01", "2028-02-01",
         "New York", 500000, "USD", None, "sample_contracts/msa.txt"),
        (3, "CloudPlatform SaaS", "SaaS Agreement", "active", "2026-03-01", "2027-03-01",
         "Delaware", 120000, "USD", "https://contracts.example.com/sa-001.pdf",
         "sample_contracts/service_agreement.txt"),
    ]
    cursor.executemany(
        "INSERT INTO contracts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
        contracts,
    )

    parties = [
        (1, 1, "Acme Corporation", "disclosing_party", "corporation", "Delaware"),
        (2, 1, "Beta Solutions LLC", "receiving_party", "llc", "California"),
        (3, 2, "TechServ Inc.", "service_provider", "corporation", "Delaware"),
        (4, 2, "GlobalCorp Industries", "client", "corporation", "New York"),
        (5, 3, "CloudPlatform Inc.", "service_provider", "corporation", "Delaware"),
        (6, 3, "DataDriven Corp.", "customer", "corporation", "Texas"),
    ]
    cursor.executemany("INSERT INTO parties VALUES (?, ?, ?, ?, ?, ?)", parties)

    clauses = [
        (1, 1, "confidentiality", "2", "Strict confidence, no third-party disclosure", "medium", None, None),
        (2, 1, "term", "4", "3-year term, 2-year survival", "low", None, None),
        (3, 1, "liability_cap", "10", "Aggregate cap of $500,000", "high", 500000, "Except willful misconduct"),
        (4, 1, "dispute_resolution", "8", "Mediation then arbitration in SF", "medium", None, None),
        (5, 2, "payment_terms", "3", "Monthly invoicing, net 30, 1.5% late fee", "medium", None, None),
        (6, 2, "ip_ownership", "4", "Work product owned by client after payment", "high", None, "Provider retains tools"),
        (7, 2, "liability_cap", "7", "12-month fees cap, excludes IP/confidentiality", "high", None, "Exclusions for breach"),
        (8, 2, "termination", "9", "30-day cure for cause, 60-day convenience", "medium", None, None),
        (9, 2, "force_majeure", "10", "Broad coverage including pandemics", "low", None, None),
        (10, 3, "sla", "3", "99.9% uptime, service credits for breach", "high", None, "Credits are sole remedy"),
        (11, 3, "data_security", "4", "AES-256, TLS 1.3, SOC 2, 72h breach notify", "critical", None, None),
        (12, 3, "liability_cap", "7", "12-month fees cap", "high", 120000, "Excludes gross negligence"),
        (13, 3, "termination", "8", "30-day cure, 90-day convenience, 30-day data export", "medium", None, None),
    ]
    cursor.executemany("INSERT INTO clauses VALUES (?, ?, ?, ?, ?, ?, ?, ?)", clauses)

    conn.commit()
    conn.close()
    print(f"Database created at {DB_PATH}")


# ── Contract SQL Toolkit ────────────────────────────────────────────────


class ContractSQLToolkit(Toolkit):
    """
    SQL-based contract research toolkit.

    Provides the agent with schema awareness and SQL execution for
    querying contract metadata. Designed for embedded SaaS usage where
    the agent needs to answer questions about a contracts database.
    """

    def __init__(self, db_path: str):
        super().__init__(name="contract_db")
        self._db_path = db_path
        self.register(self.get_schema)
        self.register(self.run_query)
        self.register(self.get_contract_summary)
        self.register(self.get_risk_overview)

    def get_schema(self) -> str:
        """
        Get the database schema for contract tables.

        Returns the full schema so you can write accurate SQL queries.
        """
        return """
Database Schema:

TABLE contracts:
  id INTEGER PRIMARY KEY
  name TEXT           -- e.g., "Acme-Beta NDA"
  contract_type TEXT  -- "NDA", "MSA", "SaaS Agreement"
  status TEXT         -- "active", "expired", "terminated"
  effective_date TEXT -- ISO date
  expiration_date TEXT
  governing_law TEXT  -- e.g., "California", "New York"
  total_value REAL    -- contract value in currency
  currency TEXT       -- "USD", "EUR"
  media_url TEXT      -- URL to contract PDF (nullable)
  file_path TEXT      -- local path to contract file (nullable)

TABLE parties:
  id INTEGER PRIMARY KEY
  contract_id INTEGER REFERENCES contracts(id)
  party_name TEXT     -- e.g., "Acme Corporation"
  party_role TEXT     -- "disclosing_party", "service_provider", "client", etc.
  entity_type TEXT    -- "corporation", "llc", "partnership"
  jurisdiction TEXT   -- incorporation state

TABLE clauses:
  id INTEGER PRIMARY KEY
  contract_id INTEGER REFERENCES contracts(id)
  clause_type TEXT    -- "confidentiality", "liability_cap", "termination", "sla", etc.
  section_number TEXT -- e.g., "4", "7.2"
  summary TEXT        -- human-readable summary
  risk_level TEXT     -- "low", "medium", "high", "critical"
  liability_cap REAL  -- dollar amount (nullable)
  notes TEXT          -- additional notes
"""

    def run_query(self, sql: str) -> str:
        """
        Execute a SQL query against the contracts database.

        IMPORTANT: Only SELECT queries are allowed. Use get_schema() first
        to understand the tables before writing queries.

        Args:
            sql: SQL SELECT query to execute.

        Returns:
            Query results as formatted text.
        """
        sql_stripped = sql.strip().upper()
        if not sql_stripped.startswith("SELECT"):
            return "[error] Only SELECT queries are allowed for safety."

        try:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(sql)
            rows = cursor.fetchall()
            conn.close()

            if not rows:
                return "Query returned no results."

            # Format as table
            columns = rows[0].keys()
            lines = [" | ".join(columns)]
            lines.append("-" * len(lines[0]))
            for row in rows:
                lines.append(" | ".join(str(row[col]) for col in columns))

            return "\n".join(lines)

        except Exception as e:
            return f"[error] SQL query failed: {e}"

    def get_contract_summary(self, contract_id: int) -> str:
        """
        Get a comprehensive summary of a specific contract.

        Args:
            contract_id: The contract ID to summarize.

        Returns:
            Formatted contract summary with parties and key clauses.
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
            "SELECT * FROM clauses WHERE contract_id = ? ORDER BY risk_level DESC",
            (contract_id,),
        ).fetchall()

        conn.close()

        parts = [
            f"Contract: {contract['name']}",
            f"Type: {contract['contract_type']} | Status: {contract['status']}",
            f"Effective: {contract['effective_date']} → {contract['expiration_date']}",
            f"Governing Law: {contract['governing_law']}",
            f"Value: {contract['currency']} {contract['total_value']:,.0f}",
            "",
            "Parties:",
        ]
        for p in parties:
            parts.append(f"  - {p['party_name']} ({p['party_role']}, {p['entity_type']}, {p['jurisdiction']})")

        parts.append("\nKey Clauses:")
        for c in clauses:
            risk = f"[{c['risk_level'].upper()}]" if c['risk_level'] else ""
            cap = f" (cap: ${c['liability_cap']:,.0f})" if c['liability_cap'] else ""
            parts.append(f"  {risk} §{c['section_number']} {c['clause_type']}: {c['summary']}{cap}")

        return "\n".join(parts)

    def get_risk_overview(self) -> str:
        """
        Get a risk overview across all contracts.

        Returns a summary of high-risk and critical clauses that need attention.
        """
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row

        rows = conn.execute("""
            SELECT c.name as contract_name, cl.clause_type, cl.section_number,
                   cl.summary, cl.risk_level, cl.liability_cap, cl.notes
            FROM clauses cl
            JOIN contracts c ON cl.contract_id = c.id
            WHERE cl.risk_level IN ('high', 'critical')
            ORDER BY
                CASE cl.risk_level WHEN 'critical' THEN 0 WHEN 'high' THEN 1 END,
                c.name
        """).fetchall()
        conn.close()

        if not rows:
            return "No high or critical risk clauses found."

        parts = ["Risk Overview — High/Critical Clauses:\n"]
        for r in rows:
            risk = r['risk_level'].upper()
            cap = f" | Cap: ${r['liability_cap']:,.0f}" if r['liability_cap'] else ""
            notes = f" | {r['notes']}" if r['notes'] else ""
            parts.append(
                f"  [{risk}] {r['contract_name']} §{r['section_number']} "
                f"{r['clause_type']}: {r['summary']}{cap}{notes}"
            )

        return "\n".join(parts)


# ── Demo ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    setup_database()

    toolkit = ContractSQLToolkit(DB_PATH)

    # Create agent with SQL toolkit
    agent = AgentHarness(
        name="contract-data-agent",
        tools=[toolkit],
        instructions=(
            "You are a contract data analyst. Use the contract_db toolkit to query "
            "the contracts database. ALWAYS call get_schema() first to understand the "
            "tables before writing SQL queries. Provide clear, structured answers."
        ),
    )

    print("=" * 60)
    print("Data Agent RAG Demo (SQL + Natural Language)")
    print("=" * 60)

    questions = [
        "What contracts do we have? Show me a summary of each.",
        "Which contracts have the highest liability exposure?",
        "Show me all critical and high-risk clauses across our contracts.",
        "Which contracts expire in the next 2 years?",
        "Compare the termination provisions across all our agreements.",
    ]

    for q in questions:
        print(f"\n[Q] {q}")
        print("-" * 60)
        agent.print_response(q, stream=True)
        print()
