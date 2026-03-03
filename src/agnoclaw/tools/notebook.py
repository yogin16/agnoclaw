"""
Notebook toolkit — read and edit Jupyter notebooks (.ipynb files).

Provides tools to read notebook contents, edit existing cells, and add new cells.

Uses nbformat for notebook parsing (lightweight, pure Python).

Usage:
    from agnoclaw.tools.notebook import NotebookToolkit

    toolkit = NotebookToolkit()
    # Tools: notebook_read, notebook_edit_cell, notebook_add_cell
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from agno.tools.toolkit import Toolkit

logger = logging.getLogger("agnoclaw.tools.notebook")


def _check_nbformat():
    try:
        import nbformat  # noqa: F401
        return True
    except ImportError:
        return False


class NotebookToolkit(Toolkit):
    """
    Jupyter notebook read/edit toolkit.

    Provides non-executing notebook manipulation — read cells, edit cells,
    and add new cells. Does not execute code cells (use bash tool for that).
    """

    def __init__(self):
        super().__init__(name="notebook")
        self.register(self.notebook_read)
        self.register(self.notebook_edit_cell)
        self.register(self.notebook_add_cell)

    def notebook_read(self, path: str) -> str:
        """
        Read a Jupyter notebook and return all cells with their outputs.

        Args:
            path: Path to the .ipynb file.

        Returns:
            Formatted representation of all notebook cells.
        """
        file_path = Path(path).expanduser().resolve()
        if not file_path.exists():
            return f"[error] Notebook not found: {path}"

        if not _check_nbformat():
            # Fallback: raw JSON parsing
            return self._read_raw(file_path)

        import nbformat

        try:
            nb = nbformat.read(str(file_path), as_version=4)
        except Exception as e:
            return f"[error] Failed to parse notebook: {e}"

        parts = [f"Notebook: {file_path.name} ({len(nb.cells)} cells)\n"]

        for i, cell in enumerate(nb.cells):
            cell_type = cell.cell_type
            source = cell.source.strip()

            parts.append(f"\n--- Cell {i} [{cell_type}] ---")
            parts.append(source)

            # Show outputs for code cells
            if cell_type == "code" and hasattr(cell, "outputs"):
                for output in cell.outputs:
                    if hasattr(output, "text"):
                        parts.append(f"\n[output]\n{output.text.strip()}")
                    elif hasattr(output, "data"):
                        if "text/plain" in output.data:
                            parts.append(f"\n[output]\n{output.data['text/plain']}")
                        elif "text/html" in output.data:
                            parts.append(f"\n[output: html]\n{output.data['text/html'][:500]}")

        return "\n".join(parts)

    def notebook_edit_cell(self, path: str, cell_index: int, new_source: str) -> str:
        """
        Replace the source of a specific cell in a notebook.

        Args:
            path: Path to the .ipynb file.
            cell_index: 0-indexed cell number to edit.
            new_source: New source content for the cell.

        Returns:
            Confirmation message.
        """
        file_path = Path(path).expanduser().resolve()
        if not file_path.exists():
            return f"[error] Notebook not found: {path}"

        if not _check_nbformat():
            return self._edit_raw(file_path, cell_index, new_source)

        import nbformat

        try:
            nb = nbformat.read(str(file_path), as_version=4)
        except Exception as e:
            return f"[error] Failed to parse notebook: {e}"

        if cell_index < 0 or cell_index >= len(nb.cells):
            return f"[error] Cell index {cell_index} out of range (0-{len(nb.cells) - 1})"

        old_type = nb.cells[cell_index].cell_type
        nb.cells[cell_index].source = new_source

        try:
            nbformat.write(nb, str(file_path))
        except Exception as e:
            return f"[error] Failed to write notebook: {e}"

        return f"Updated cell {cell_index} [{old_type}] in {file_path.name}"

    def notebook_add_cell(
        self, path: str, cell_type: str, source: str, position: int = -1
    ) -> str:
        """
        Add a new cell to a notebook.

        Args:
            path: Path to the .ipynb file.
            cell_type: Cell type: 'code' or 'markdown'.
            source: Cell source content.
            position: Insert position (0-indexed). -1 appends to end.

        Returns:
            Confirmation message with the new cell index.
        """
        file_path = Path(path).expanduser().resolve()
        if not file_path.exists():
            return f"[error] Notebook not found: {path}"

        if cell_type not in ("code", "markdown"):
            return f"[error] cell_type must be 'code' or 'markdown', got '{cell_type}'"

        if not _check_nbformat():
            return self._add_raw(file_path, cell_type, source, position)

        import nbformat

        try:
            nb = nbformat.read(str(file_path), as_version=4)
        except Exception as e:
            return f"[error] Failed to parse notebook: {e}"

        if cell_type == "code":
            new_cell = nbformat.v4.new_code_cell(source=source)
        else:
            new_cell = nbformat.v4.new_markdown_cell(source=source)

        if position < 0 or position >= len(nb.cells):
            nb.cells.append(new_cell)
            idx = len(nb.cells) - 1
        else:
            nb.cells.insert(position, new_cell)
            idx = position

        try:
            nbformat.write(nb, str(file_path))
        except Exception as e:
            return f"[error] Failed to write notebook: {e}"

        return f"Added {cell_type} cell at index {idx} in {file_path.name}"

    # ── Fallback raw JSON methods (when nbformat not installed) ──────────────

    @staticmethod
    def _read_raw(path: Path) -> str:
        """Read notebook using raw JSON parsing."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            return f"[error] Failed to read notebook: {e}"

        cells = data.get("cells", [])
        parts = [f"Notebook: {path.name} ({len(cells)} cells)\n"]

        for i, cell in enumerate(cells):
            cell_type = cell.get("cell_type", "unknown")
            source = "".join(cell.get("source", []))
            parts.append(f"\n--- Cell {i} [{cell_type}] ---")
            parts.append(source.strip())

            if cell_type == "code":
                for output in cell.get("outputs", []):
                    text = output.get("text", "")
                    if isinstance(text, list):
                        text = "".join(text)
                    if text:
                        parts.append(f"\n[output]\n{text.strip()}")

        return "\n".join(parts)

    @staticmethod
    def _edit_raw(path: Path, cell_index: int, new_source: str) -> str:
        """Edit notebook cell using raw JSON."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            return f"[error] Failed to read notebook: {e}"

        cells = data.get("cells", [])
        if cell_index < 0 or cell_index >= len(cells):
            return f"[error] Cell index {cell_index} out of range"

        cells[cell_index]["source"] = new_source.split("\n")
        path.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        return f"Updated cell {cell_index} in {path.name}"

    @staticmethod
    def _add_raw(path: Path, cell_type: str, source: str, position: int) -> str:
        """Add cell using raw JSON."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            return f"[error] Failed to read notebook: {e}"

        cells = data.get("cells", [])
        new_cell = {
            "cell_type": cell_type,
            "metadata": {},
            "source": source.split("\n"),
        }
        if cell_type == "code":
            new_cell["outputs"] = []
            new_cell["execution_count"] = None

        if position < 0 or position >= len(cells):
            cells.append(new_cell)
            idx = len(cells) - 1
        else:
            cells.insert(position, new_cell)
            idx = position

        path.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        return f"Added {cell_type} cell at index {idx} in {path.name}"
