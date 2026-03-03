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
