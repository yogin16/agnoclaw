"""
Example: ContractToolkit — wrapping a knowledge base as an Agno Toolkit for AgentHarness

This pattern is key for embedded library usage: wrap your domain-specific
data sources as a Toolkit that AgentHarness can use alongside its default tools.

Usage:
    from examples.legal_rag.custom_toolkit import ContractToolkit

    toolkit = ContractToolkit(contracts_dir="/path/to/contracts")
    agent = AgentHarness(tools=[toolkit])
    agent.print_response("What are the termination clauses?")
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from agno.tools.toolkit import Toolkit


class ContractToolkit(Toolkit):
    """
    Domain-specific toolkit wrapping a contract knowledge base.

    Provides tools for:
      - Searching contracts by keyword/clause
      - Getting full contract text
      - Listing available contracts
      - Extracting specific sections

    This toolkit can be used with AgentHarness or standalone Agno Agent.
    For embedded SaaS use, initialize with your data source and pass to AgentHarness.
    """

    def __init__(
        self,
        contracts_dir: str | Path = "",
        vector_db=None,
        knowledge_base=None,
    ):
        super().__init__(name="contracts")
        self._contracts_dir = Path(contracts_dir).expanduser().resolve() if contracts_dir else None
        self._vector_db = vector_db
        self._knowledge_base = knowledge_base
        self._contract_cache: dict[str, str] = {}

        self.register(self.search_contracts)
        self.register(self.get_contract)
        self.register(self.list_contracts)
        self.register(self.extract_section)

        # Pre-load contracts if directory provided
        if self._contracts_dir and self._contracts_dir.exists():
            self._load_contracts()

    def _load_contracts(self) -> None:
        """Load contracts from the directory into memory cache."""
        if not self._contracts_dir:
            return
        for f in self._contracts_dir.glob("*"):
            if f.suffix in (".txt", ".md"):
                self._contract_cache[f.stem] = f.read_text(encoding="utf-8")

    def search_contracts(self, query: str, limit: int = 5) -> str:
        """
        Search across all contracts for relevant clauses or terms.

        Uses vector search if a knowledge base is configured, otherwise
        falls back to keyword search across loaded contracts.

        Args:
            query: Search query (e.g., "termination clause", "liability cap").
            limit: Maximum number of results.

        Returns:
            Matching contract excerpts with source references.
        """
        # Try vector search first
        if self._knowledge_base:
            try:
                results = self._knowledge_base.search(query, num_documents=limit)
                if results:
                    parts = []
                    for i, doc in enumerate(results, 1):
                        source = getattr(doc, 'name', 'unknown')
                        content = doc.content if hasattr(doc, 'content') else str(doc)
                        parts.append(f"[{i}] Source: {source}\n{content[:500]}")
                    return "\n\n".join(parts)
            except Exception as e:
                pass  # Fall through to keyword search

        # Keyword search fallback
        results = []
        query_lower = query.lower()
        for name, text in self._contract_cache.items():
            lines = text.split("\n")
            for i, line in enumerate(lines):
                if query_lower in line.lower():
                    # Include surrounding context
                    start = max(0, i - 2)
                    end = min(len(lines), i + 3)
                    context = "\n".join(lines[start:end])
                    results.append(f"[{name}] (line {i+1}):\n{context}")
                    if len(results) >= limit:
                        break
            if len(results) >= limit:
                break

        return "\n\n".join(results) if results else f"No results found for: {query}"

    def get_contract(self, name: str) -> str:
        """
        Get the full text of a specific contract.

        Args:
            name: Contract name (filename without extension, e.g., "nda", "msa").

        Returns:
            Full contract text, or error if not found.
        """
        if name in self._contract_cache:
            return self._contract_cache[name]

        # Try with common extensions
        if self._contracts_dir:
            for ext in (".txt", ".md", ".pdf"):
                path = self._contracts_dir / f"{name}{ext}"
                if path.exists():
                    text = path.read_text(encoding="utf-8")
                    self._contract_cache[name] = text
                    return text

        return f"[error] Contract '{name}' not found. Use list_contracts to see available contracts."

    def list_contracts(self) -> str:
        """
        List all available contracts in the knowledge base.

        Returns:
            Formatted list of contract names and sizes.
        """
        if not self._contract_cache:
            self._load_contracts()

        if not self._contract_cache:
            return "No contracts loaded."

        lines = ["Available contracts:"]
        for name, text in sorted(self._contract_cache.items()):
            lines.append(f"  - {name} ({len(text)} chars, ~{len(text.split())} words)")
        return "\n".join(lines)

    def extract_section(self, contract_name: str, section_query: str) -> str:
        """
        Extract a specific section from a contract.

        Args:
            contract_name: Name of the contract.
            section_query: Section to find (e.g., "TERMINATION", "LIABILITY", "Section 4").

        Returns:
            The matching section text, or error if not found.
        """
        text = self.get_contract(contract_name)
        if text.startswith("[error]"):
            return text

        import re
        query_upper = section_query.upper()

        # Try numbered section match: "4. TERM" or "Section 4"
        num_match = re.search(r"\d+", section_query)
        if num_match:
            num = num_match.group()
            pattern = re.compile(
                rf"^{num}\.\s+.*$",
                re.MULTILINE,
            )
            match = pattern.search(text)
            if match:
                start = match.start()
                # Find the next section
                next_section = re.search(rf"^\d+\.\s+", text[match.end():], re.MULTILINE)
                end = match.end() + next_section.start() if next_section else len(text)
                return text[start:end].strip()

        # Try keyword match on section headers
        lines = text.split("\n")
        section_start = None
        for i, line in enumerate(lines):
            if query_upper in line.upper() and (
                line.strip().startswith(tuple("0123456789"))
                or line.strip().isupper()
            ):
                section_start = i
                break

        if section_start is not None:
            # Find end of section
            section_end = len(lines)
            for i in range(section_start + 1, len(lines)):
                line = lines[i].strip()
                if line and (
                    re.match(r"^\d+\.\s+[A-Z]", line)
                    or (line.isupper() and len(line) > 3)
                ):
                    section_end = i
                    break
            return "\n".join(lines[section_start:section_end]).strip()

        return f"Section '{section_query}' not found in {contract_name}."


# ── Demo: use with AgentHarness ─────────────────────────────────────────

if __name__ == "__main__":
    contracts_dir = Path(__file__).parent / "sample_contracts"

    toolkit = ContractToolkit(contracts_dir=contracts_dir)

    # Use as embedded library — AgentHarness with custom toolkit
    agent = AgentHarness(
        name="contract-assistant",
        tools=[toolkit],
        instructions=(
            "You are a contract analysis assistant. Use the contracts toolkit "
            "to search, read, and analyze legal contracts. Always cite specific "
            "sections and clauses in your answers."
        ),
    )

    print("=" * 60)
    print("Contract Toolkit Demo (Embedded Library Pattern)")
    print("=" * 60)

    questions = [
        "What contracts are available?",
        "What is the liability cap in the NDA?",
        "Extract the termination section from the MSA.",
        "Compare the dispute resolution clauses across all contracts.",
    ]

    for q in questions:
        print(f"\n[Q] {q}")
        print("-" * 60)
        agent.print_response(q, stream=True)
        print()
