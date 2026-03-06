"""Tests for the notebook toolkit."""

import json
from pathlib import Path

import pytest

from agnoclaw.tools.notebook import NotebookToolkit


@pytest.fixture
def notebook_toolkit():
    return NotebookToolkit()


@pytest.fixture
def sample_notebook(tmp_path) -> Path:
    """Create a minimal Jupyter notebook."""
    nb = {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {"kernelspec": {"language": "python"}},
        "cells": [
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": ["# Test Notebook\n", "This is a test."],
            },
            {
                "cell_type": "code",
                "metadata": {},
                "source": ["print('hello world')"],
                "outputs": [{"text": ["hello world\n"], "output_type": "stream", "name": "stdout"}],
                "execution_count": 1,
            },
            {
                "cell_type": "code",
                "metadata": {},
                "source": ["x = 42"],
                "outputs": [],
                "execution_count": 2,
            },
        ],
    }
    path = tmp_path / "test.ipynb"
    path.write_text(json.dumps(nb), encoding="utf-8")
    return path


def test_notebook_toolkit_registers_tools(notebook_toolkit):
    expected = {"notebook_read", "notebook_edit_cell", "notebook_add_cell"}
    registered = set(notebook_toolkit.functions.keys())
    assert expected.issubset(registered)


def test_notebook_read(notebook_toolkit, sample_notebook):
    result = notebook_toolkit.notebook_read(str(sample_notebook))

    assert "3 cells" in result
    assert "Test Notebook" in result
    assert "print('hello world')" in result
    assert "hello world" in result  # output
    assert "x = 42" in result


def test_notebook_read_not_found(notebook_toolkit):
    result = notebook_toolkit.notebook_read("/nonexistent/notebook.ipynb")
    assert "[error]" in result


def test_notebook_edit_cell(notebook_toolkit, sample_notebook):
    result = notebook_toolkit.notebook_edit_cell(
        str(sample_notebook), cell_index=2, new_source="x = 100"
    )

    assert "Updated cell 2" in result

    # Verify the edit persisted
    data = json.loads(sample_notebook.read_text())
    source = data["cells"][2]["source"]
    if isinstance(source, list):
        source = "".join(source)
    assert "100" in source


def test_notebook_edit_cell_out_of_range(notebook_toolkit, sample_notebook):
    result = notebook_toolkit.notebook_edit_cell(
        str(sample_notebook), cell_index=99, new_source="nope"
    )
    assert "[error]" in result


def test_notebook_add_cell(notebook_toolkit, sample_notebook):
    result = notebook_toolkit.notebook_add_cell(
        str(sample_notebook), cell_type="code", source="y = 99"
    )

    assert "Added code cell" in result

    # Verify the cell was added
    data = json.loads(sample_notebook.read_text())
    assert len(data["cells"]) == 4


def test_notebook_add_cell_at_position(notebook_toolkit, sample_notebook):
    result = notebook_toolkit.notebook_add_cell(
        str(sample_notebook), cell_type="markdown", source="## Section 2", position=1
    )

    assert "index 1" in result

    data = json.loads(sample_notebook.read_text())
    assert len(data["cells"]) == 4
    # New cell should be at position 1
    new_cell = data["cells"][1]
    source = "".join(new_cell["source"]) if isinstance(new_cell["source"], list) else new_cell["source"]
    assert "Section 2" in source


def test_notebook_add_cell_invalid_type(notebook_toolkit, sample_notebook):
    result = notebook_toolkit.notebook_add_cell(
        str(sample_notebook), cell_type="invalid", source="test"
    )
    assert "[error]" in result


# ── Raw fallback tests (when nbformat is NOT installed) ─────────────────


def test_read_raw_basic(notebook_toolkit, sample_notebook):
    """_read_raw parses notebook JSON directly."""
    from agnoclaw.tools.notebook import NotebookToolkit

    result = NotebookToolkit._read_raw(sample_notebook)
    assert "3 cells" in result
    assert "Test Notebook" in result
    assert "print('hello world')" in result
    assert "hello world" in result  # output text
    assert "x = 42" in result


def test_read_raw_output_list_text(tmp_path):
    """_read_raw handles output text as a list of strings."""
    nb = {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {},
        "cells": [
            {
                "cell_type": "code",
                "metadata": {},
                "source": ["print('hi')"],
                "outputs": [{"text": ["hi", "\n"], "output_type": "stream"}],
                "execution_count": 1,
            },
        ],
    }
    path = tmp_path / "test.ipynb"
    path.write_text(json.dumps(nb), encoding="utf-8")

    result = NotebookToolkit._read_raw(path)
    assert "hi" in result
    assert "[output]" in result


def test_read_raw_invalid_json(tmp_path):
    """_read_raw returns error for invalid JSON."""
    path = tmp_path / "bad.ipynb"
    path.write_text("not json at all", encoding="utf-8")

    result = NotebookToolkit._read_raw(path)
    assert "[error]" in result


def test_edit_raw_success(tmp_path):
    """_edit_raw replaces cell source via raw JSON."""
    nb = {
        "nbformat": 4,
        "metadata": {},
        "cells": [
            {"cell_type": "code", "source": ["old code"], "metadata": {}, "outputs": []},
        ],
    }
    path = tmp_path / "test.ipynb"
    path.write_text(json.dumps(nb), encoding="utf-8")

    result = NotebookToolkit._edit_raw(path, 0, "new code")
    assert "Updated cell 0" in result

    # Verify on disk
    data = json.loads(path.read_text())
    assert data["cells"][0]["source"] == ["new code"]


def test_edit_raw_out_of_range(tmp_path):
    """_edit_raw returns error for out-of-range cell index."""
    nb = {"cells": [{"cell_type": "code", "source": ["x"], "metadata": {}}]}
    path = tmp_path / "test.ipynb"
    path.write_text(json.dumps(nb), encoding="utf-8")

    result = NotebookToolkit._edit_raw(path, 5, "new")
    assert "[error]" in result


def test_edit_raw_invalid_json(tmp_path):
    """_edit_raw returns error for invalid notebook JSON."""
    path = tmp_path / "bad.ipynb"
    path.write_text("not json", encoding="utf-8")

    result = NotebookToolkit._edit_raw(path, 0, "new")
    assert "[error]" in result


def test_add_raw_code_cell_append(tmp_path):
    """_add_raw appends a code cell with outputs and execution_count."""
    nb = {
        "nbformat": 4,
        "metadata": {},
        "cells": [{"cell_type": "markdown", "source": ["# Hi"], "metadata": {}}],
    }
    path = tmp_path / "test.ipynb"
    path.write_text(json.dumps(nb), encoding="utf-8")

    result = NotebookToolkit._add_raw(path, "code", "x = 1", -1)
    assert "Added code cell at index 1" in result

    data = json.loads(path.read_text())
    assert len(data["cells"]) == 2
    new_cell = data["cells"][1]
    assert new_cell["cell_type"] == "code"
    assert new_cell["outputs"] == []
    assert new_cell["execution_count"] is None


def test_add_raw_markdown_cell_at_position(tmp_path):
    """_add_raw inserts a markdown cell at a specific position."""
    nb = {
        "nbformat": 4,
        "metadata": {},
        "cells": [
            {"cell_type": "code", "source": ["a"], "metadata": {}},
            {"cell_type": "code", "source": ["b"], "metadata": {}},
        ],
    }
    path = tmp_path / "test.ipynb"
    path.write_text(json.dumps(nb), encoding="utf-8")

    result = NotebookToolkit._add_raw(path, "markdown", "# Title", 0)
    assert "index 0" in result

    data = json.loads(path.read_text())
    assert len(data["cells"]) == 3
    assert data["cells"][0]["cell_type"] == "markdown"


def test_add_raw_invalid_json(tmp_path):
    """_add_raw returns error for invalid notebook JSON."""
    path = tmp_path / "bad.ipynb"
    path.write_text("not json", encoding="utf-8")

    result = NotebookToolkit._add_raw(path, "code", "x", -1)
    assert "[error]" in result


# ── Fallback routing tests (mock _check_nbformat to False) ──────────────

from unittest.mock import patch


def test_notebook_read_uses_raw_fallback(notebook_toolkit, sample_notebook):
    """notebook_read falls back to _read_raw when nbformat unavailable."""
    with patch("agnoclaw.tools.notebook._check_nbformat", return_value=False):
        result = notebook_toolkit.notebook_read(str(sample_notebook))
    assert "3 cells" in result
    assert "Test Notebook" in result


def test_notebook_edit_uses_raw_fallback(tmp_path):
    """notebook_edit_cell falls back to _edit_raw when nbformat unavailable."""
    nb = {
        "nbformat": 4,
        "metadata": {},
        "cells": [{"cell_type": "code", "source": ["old"], "metadata": {}, "outputs": []}],
    }
    path = tmp_path / "test.ipynb"
    path.write_text(json.dumps(nb), encoding="utf-8")

    toolkit = NotebookToolkit()
    with patch("agnoclaw.tools.notebook._check_nbformat", return_value=False):
        result = toolkit.notebook_edit_cell(str(path), 0, "new")
    assert "Updated cell 0" in result


def test_notebook_add_uses_raw_fallback(tmp_path):
    """notebook_add_cell falls back to _add_raw when nbformat unavailable."""
    nb = {
        "nbformat": 4,
        "metadata": {},
        "cells": [{"cell_type": "code", "source": ["x"], "metadata": {}, "outputs": []}],
    }
    path = tmp_path / "test.ipynb"
    path.write_text(json.dumps(nb), encoding="utf-8")

    toolkit = NotebookToolkit()
    with patch("agnoclaw.tools.notebook._check_nbformat", return_value=False):
        result = toolkit.notebook_add_cell(str(path), "code", "y = 1")
    assert "Added code cell" in result


# ── Error branch tests ──────────────────────────────────────────────────


def test_notebook_edit_cell_not_found(notebook_toolkit):
    """notebook_edit_cell returns error for missing file."""
    result = notebook_toolkit.notebook_edit_cell("/nonexistent.ipynb", 0, "new")
    assert "[error]" in result


def test_notebook_add_cell_not_found(notebook_toolkit):
    """notebook_add_cell returns error for missing file."""
    result = notebook_toolkit.notebook_add_cell("/nonexistent.ipynb", "code", "x")
    assert "[error]" in result
