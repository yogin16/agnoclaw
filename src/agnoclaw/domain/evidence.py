"""Versioned text evidence with conservative, source-preserving quote location."""

from __future__ import annotations

import hashlib
from array import array
from dataclasses import dataclass
from typing import Literal

from ._contracts import Record, digest_string, identifier
from .objects import ObjectRef

_LIGATURES = str.maketrans(
    {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"}
)


@dataclass(frozen=True)
class TextSource(Record):
    """One host-authorized text rendition; digest binds this exact rendition."""

    reference: ObjectRef
    text: str
    extractor_version: str = "plain-text-v1"

    def __post_init__(self) -> None:
        identifier(self.extractor_version, "extractor_version")
        if not isinstance(self.text, str) or len(self.text.encode("utf-8")) > 4_000_000:
            raise ValueError("source text must fit the 4 MB rendition budget")

    @property
    def text_digest(self) -> str:
        return f"sha256:{hashlib.sha256(self.text.encode('utf-8')).hexdigest()}"


@dataclass(frozen=True)
class EvidenceRef(Record):
    """Offsets are Unicode character offsets into the original text rendition."""

    source: ObjectRef
    source_digest: str
    extractor_version: str
    start: int
    end: int
    quote: str

    def __post_init__(self) -> None:
        digest_string(self.source_digest)
        identifier(self.extractor_version, "extractor_version")
        if type(self.start) is not int or type(self.end) is not int:
            raise ValueError("evidence offsets must be integers")
        if self.start < 0 or self.end <= self.start:
            raise ValueError("evidence offsets must describe a non-empty span")
        if not isinstance(self.quote, str) or not self.quote.strip() or len(self.quote) > 65_536:
            raise ValueError("quote must be non-empty and at most 65536 characters")


@dataclass(frozen=True)
class QuoteMatch(Record):
    status: Literal["matched", "missing", "ambiguous"]
    evidence: EvidenceRef | None = None


def _normalize(text: str) -> tuple[str, array, array]:
    """Only collapse whitespace and expand known ligatures; never fuzzy-match numbers."""
    chars: list[str] = []
    # Compact integer arrays avoid allocating millions of Python tuples for a document.
    starts, ends = array("I"), array("I")
    for index, char in enumerate(text):
        if char.isspace():
            if chars and chars[-1] == " ":
                ends[-1] = index + 1
            else:
                chars.append(" ")
                starts.append(index)
                ends.append(index + 1)
        else:
            expanded = char.translate(_LIGATURES)
            chars.extend(expanded)
            starts.extend([index] * len(expanded))
            ends.extend([index + 1] * len(expanded))
    return "".join(chars), starts, ends


def locate_quote(source: TextSource, quote: str) -> QuoteMatch:
    """Return a unique location, or abstain. A match does not prove claim support."""
    if not isinstance(quote, str) or not quote.strip() or len(quote) > 65_536:
        raise ValueError("quote must be non-empty and at most 65536 characters")
    text, starts, ends = _normalize(source.text)
    needle = _normalize(quote)[0].strip()
    positions: list[int] = []
    position = text.find(needle)
    while position >= 0:
        end = position + len(needle)
        # A quote cannot begin/end halfway through an expanded source ligature.
        start_ok = position == 0 or starts[position - 1] != starts[position]
        end_ok = end == len(ends) or ends[end - 1] != ends[end]
        if start_ok and end_ok:
            positions.append(position)
            if len(positions) > 1:
                return QuoteMatch("ambiguous")
        position = text.find(needle, position + 1)
    if not positions:
        return QuoteMatch("missing")
    position = positions[0]
    return QuoteMatch(
        "matched",
        EvidenceRef(
            source=source.reference,
            source_digest=source.text_digest,
            extractor_version=source.extractor_version,
            start=starts[position],
            end=ends[position + len(needle) - 1],
            quote=quote,
        ),
    )


def quote_is_located(evidence: EvidenceRef, source: TextSource) -> bool:
    """Recheck reference, version, digest, bounds, and quote against host-loaded text."""
    return (
        evidence.source == source.reference
        and evidence.source_digest == source.text_digest
        and evidence.extractor_version == source.extractor_version
        and evidence.end <= len(source.text)
        and _normalize(source.text[evidence.start : evidence.end])[0].strip()
        == _normalize(evidence.quote)[0].strip()
    )
