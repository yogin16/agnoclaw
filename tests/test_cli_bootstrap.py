"""Tests for the thin CLI bootstrap entrypoint."""

from __future__ import annotations

import importlib


def test_bootstrap_reports_missing_cli_dependencies(monkeypatch, capsys):
    from agnoclaw.cli import bootstrap

    original_import_module = importlib.import_module

    def fake_import_module(name):
        if name == "agnoclaw.cli.main":
            raise ImportError("CLI dependencies not installed. Install with: pip install 'agnoclaw[cli]'")
        return original_import_module(name)

    monkeypatch.setattr(importlib, "import_module", fake_import_module)
    assert bootstrap.main([]) == 1
    captured = capsys.readouterr()
    assert "agnoclaw[cli]" in captured.err
