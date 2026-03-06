"""
Example: Clause-Aware Chunking — split contracts on clause/section boundaries

Standard chunking (by token count) breaks mid-sentence or mid-clause.
This custom chunker preserves legal document structure by splitting on
section boundaries while maintaining hierarchy metadata.

Usage:
    from examples.legal_rag.clause_aware_chunking import ClauseAwareChunker
    chunks = ClauseAwareChunker().chunk(contract_text)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class ContractChunk:
    """A single chunk from a contract, with structural metadata."""

    text: str
    section_number: str = ""
    section_title: str = ""
    parent_section: str = ""
    chunk_index: int = 0
    source_file: str = ""


class ClauseAwareChunker:
    """
    Split legal contracts on clause/section boundaries.

    Recognizes common legal document patterns:
      - Numbered sections: "1.", "2.", "1.1", "Section 1"
      - Lettered subsections: "(a)", "(b)", "(i)", "(ii)"
      - ALL-CAPS headers: "GOVERNING LAW", "LIMITATION OF LIABILITY"
      - Markdown-style headers: "# Section", "## Subsection"

    Chunks preserve the full section context (title + body) and
    include metadata for hierarchical navigation.
    """

    # Pattern: numbered sections like "1. TITLE", "2. TITLE", "10. TITLE"
    SECTION_PATTERN = re.compile(
        r"^(\d+)\.\s+([A-Z][A-Z\s&,]+?)$",
        re.MULTILINE,
    )

    # Pattern: subsections like "(a)", "(b)", "(i)", "(ii)"
    SUBSECTION_PATTERN = re.compile(
        r"^\(([a-z]|[ivx]+)\)\s+",
        re.MULTILINE,
    )

    # Pattern: ALL-CAPS headers on their own line
    CAPS_HEADER_PATTERN = re.compile(
        r"^([A-Z][A-Z\s&,]{3,})$",
        re.MULTILINE,
    )

    def __init__(self, max_chunk_size: int = 2000, overlap_sentences: int = 1):
        self.max_chunk_size = max_chunk_size
        self.overlap_sentences = overlap_sentences

    def chunk(self, text: str, source_file: str = "") -> list[ContractChunk]:
        """
        Split contract text into clause-aware chunks.

        Args:
            text: Full contract text.
            source_file: Source filename for metadata.

        Returns:
            List of ContractChunk with structural metadata.
        """
        sections = self._split_into_sections(text)
        chunks = []

        for i, (section_num, section_title, section_body) in enumerate(sections):
            # If section is small enough, keep as one chunk
            if len(section_body) <= self.max_chunk_size:
                chunks.append(ContractChunk(
                    text=f"{section_num}. {section_title}\n\n{section_body}".strip(),
                    section_number=section_num,
                    section_title=section_title,
                    chunk_index=len(chunks),
                    source_file=source_file,
                ))
            else:
                # Split large sections on subsection boundaries
                sub_chunks = self._split_section(section_body, section_num, section_title)
                for sub in sub_chunks:
                    sub.chunk_index = len(chunks)
                    sub.source_file = source_file
                    chunks.append(sub)

        return chunks

    def _split_into_sections(self, text: str) -> list[tuple[str, str, str]]:
        """Split text into (section_number, section_title, section_body) tuples."""
        sections = []
        matches = list(self.SECTION_PATTERN.finditer(text))

        if not matches:
            # No numbered sections found — return as single chunk
            return [("0", "Full Document", text)]

        for i, match in enumerate(matches):
            section_num = match.group(1)
            section_title = match.group(2).strip()
            start = match.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            section_body = text[start:end].strip()
            sections.append((section_num, section_title, section_body))

        # Include any preamble before the first section
        if matches and matches[0].start() > 0:
            preamble = text[:matches[0].start()].strip()
            if preamble:
                sections.insert(0, ("0", "Preamble", preamble))

        return sections

    def _split_section(
        self, body: str, section_num: str, section_title: str
    ) -> list[ContractChunk]:
        """Split a large section into smaller chunks on subsection boundaries."""
        chunks = []
        # Try splitting on subsection markers
        parts = self.SUBSECTION_PATTERN.split(body)

        current_text = ""
        for part in parts:
            if len(current_text) + len(part) > self.max_chunk_size and current_text:
                chunks.append(ContractChunk(
                    text=f"{section_num}. {section_title}\n\n{current_text.strip()}",
                    section_number=section_num,
                    section_title=section_title,
                    parent_section=section_num,
                ))
                # Overlap: keep last sentence
                sentences = current_text.strip().split(".")
                current_text = sentences[-1] + "." if sentences else ""
            current_text += part

        if current_text.strip():
            chunks.append(ContractChunk(
                text=f"{section_num}. {section_title}\n\n{current_text.strip()}",
                section_number=section_num,
                section_title=section_title,
                parent_section=section_num,
            ))

        return chunks


# ── Demo ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from pathlib import Path

    contracts_dir = Path(__file__).parent / "sample_contracts"
    chunker = ClauseAwareChunker(max_chunk_size=1500)

    for contract_file in sorted(contracts_dir.glob("*.txt")):
        text = contract_file.read_text(encoding="utf-8")
        chunks = chunker.chunk(text, source_file=contract_file.name)

        print(f"\n{'=' * 60}")
        print(f"{contract_file.name}: {len(chunks)} chunks")
        print(f"{'=' * 60}")

        for chunk in chunks:
            print(f"\n  [{chunk.section_number}] {chunk.section_title}")
            print(f"  {len(chunk.text)} chars")
            print(f"  Preview: {chunk.text[:100]}...")
