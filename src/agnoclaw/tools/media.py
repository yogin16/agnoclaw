"""
Media toolkit — read images and PDFs for multimodal or text-based analysis.

Provides tools for extracting text from PDFs and reading images as base64
for multimodal model consumption.

Optional extra: agnoclaw[media] → pymupdf>=1.23.0 (fallback: pypdf)

Usage:
    from agnoclaw.tools.media import MediaToolkit

    toolkit = MediaToolkit()
    # Tools: read_image, read_pdf
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Optional

from agno.tools.toolkit import Toolkit

logger = logging.getLogger("agnoclaw.tools.media")


class MediaToolkit(Toolkit):
    """
    Media reading toolkit for images and PDFs.

    Provides text extraction from PDFs (via PyMuPDF or pypdf fallback)
    and base64 encoding for images (for multimodal model input).
    """

    def __init__(self):
        super().__init__(name="media")
        self.register(self.read_image)
        self.register(self.read_pdf)

    def read_image(self, path: str) -> str:
        """
        Read an image file and return its base64-encoded content.

        Suitable for passing to multimodal models that accept image input.

        Args:
            path: Path to the image file (PNG, JPG, JPEG, GIF, WEBP, BMP).

        Returns:
            Base64-encoded image with data URI prefix, or error message.
        """
        file_path = Path(path).expanduser().resolve()
        if not file_path.exists():
            return f"[error] Image not found: {path}"

        suffix = file_path.suffix.lower()
        mime_types = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".bmp": "image/bmp",
            ".svg": "image/svg+xml",
        }
        mime = mime_types.get(suffix, "application/octet-stream")

        try:
            data = file_path.read_bytes()
            b64 = base64.b64encode(data).decode("ascii")
            size_kb = len(data) / 1024
            return (
                f"Image: {file_path.name} ({mime}, {size_kb:.1f} KB)\n"
                f"data:{mime};base64,{b64}"
            )
        except Exception as e:
            return f"[error] Failed to read image: {e}"

    def read_pdf(self, path: str, pages: str = "") -> str:
        """
        Extract text content from a PDF file.

        Args:
            path: Path to the PDF file.
            pages: Optional page range (e.g., "1-5", "3", "1,3,5"). Empty for all pages.

        Returns:
            Extracted text content, or error message.
        """
        file_path = Path(path).expanduser().resolve()
        if not file_path.exists():
            return f"[error] PDF not found: {path}"

        page_indices = self._parse_page_range(pages) if pages else None

        # Try PyMuPDF first (faster, better quality)
        try:
            return self._read_pdf_pymupdf(file_path, page_indices)
        except ImportError:
            pass
        except Exception as e:
            return f"[error] Failed to read PDF with pymupdf: {e}"

        # Fallback to pypdf
        try:
            return self._read_pdf_pypdf(file_path, page_indices)
        except ImportError:
            return (
                "[error] No PDF reader available. Install one of:\n"
                "  pip install pymupdf    (recommended)\n"
                "  pip install pypdf"
            )
        except Exception as e:
            return f"[error] Failed to read PDF with pypdf: {e}"

    @staticmethod
    def _read_pdf_pymupdf(path: Path, page_indices: Optional[list[int]] = None) -> str:
        """Extract text using PyMuPDF (fitz)."""
        import fitz  # pymupdf

        doc = fitz.open(str(path))
        total_pages = len(doc)
        parts = [f"PDF: {path.name} ({total_pages} pages)\n"]

        indices = page_indices or range(total_pages)
        for i in indices:
            if 0 <= i < total_pages:
                page = doc[i]
                text = page.get_text().strip()
                if text:
                    parts.append(f"\n--- Page {i + 1} ---\n{text}")

        doc.close()
        return "\n".join(parts)

    @staticmethod
    def _read_pdf_pypdf(path: Path, page_indices: Optional[list[int]] = None) -> str:
        """Extract text using pypdf (fallback)."""
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        total_pages = len(reader.pages)
        parts = [f"PDF: {path.name} ({total_pages} pages)\n"]

        indices = page_indices or range(total_pages)
        for i in indices:
            if 0 <= i < total_pages:
                text = reader.pages[i].extract_text().strip()
                if text:
                    parts.append(f"\n--- Page {i + 1} ---\n{text}")

        return "\n".join(parts)

    @staticmethod
    def _parse_page_range(pages_str: str) -> list[int]:
        """Parse a page range string into 0-indexed page indices."""
        indices = []
        for part in pages_str.split(","):
            part = part.strip()
            if "-" in part:
                start, end = part.split("-", 1)
                start_idx = int(start.strip()) - 1
                end_idx = int(end.strip())
                indices.extend(range(start_idx, end_idx))
            else:
                indices.append(int(part) - 1)
        return sorted(set(indices))
