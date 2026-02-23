"""Tests for the memory management utilities."""

import pytest
from unittest.mock import MagicMock, patch


# ── build_memory_manager tests ───────────────────────────────────────────


def test_build_memory_manager_returns_object():
    """build_memory_manager should return a MemoryManager without error."""
    from agnoclaw.memory import build_memory_manager

    with patch("agno.memory.MemoryManager") as mock_mm:
        mock_mm.return_value = MagicMock()
        with patch("agnoclaw.agent._make_model") as mock_model:
            mock_model.return_value = MagicMock()
            result = build_memory_manager()
            assert mock_mm.called


def test_build_memory_manager_uses_haiku_by_default():
    from agnoclaw.memory import build_memory_manager

    with patch("agno.memory.MemoryManager"):
        with patch("agnoclaw.agent._make_model") as mock_model:
            mock_model.return_value = MagicMock()
            build_memory_manager()
            mock_model.assert_called_once_with("claude-haiku-4-5-20251001", "anthropic")


def test_build_memory_manager_accepts_custom_model():
    from agnoclaw.memory import build_memory_manager

    with patch("agno.memory.MemoryManager"):
        with patch("agnoclaw.agent._make_model") as mock_model:
            mock_model.return_value = MagicMock()
            build_memory_manager(model_id="gpt-4o-mini", provider="openai")
            mock_model.assert_called_once_with("gpt-4o-mini", "openai")


def test_build_memory_manager_passes_db():
    from agnoclaw.memory import build_memory_manager

    mock_db = MagicMock()

    with patch("agno.memory.MemoryManager") as mock_mm:
        mock_mm.return_value = MagicMock()
        with patch("agnoclaw.agent._make_model") as mock_model:
            mock_model.return_value = MagicMock()
            build_memory_manager(db=mock_db)
            call_kwargs = mock_mm.call_args[1]
            assert call_kwargs.get("db") is mock_db


def test_build_memory_manager_no_db_kwarg_without_db():
    """When db=None, 'db' key should not be passed to MemoryManager."""
    from agnoclaw.memory import build_memory_manager

    with patch("agno.memory.MemoryManager") as mock_mm:
        mock_mm.return_value = MagicMock()
        with patch("agnoclaw.agent._make_model") as mock_model:
            mock_model.return_value = MagicMock()
            build_memory_manager(db=None)
            call_kwargs = mock_mm.call_args[1]
            assert "db" not in call_kwargs


def test_build_memory_manager_extra_instructions():
    from agnoclaw.memory import build_memory_manager

    with patch("agno.memory.MemoryManager") as mock_mm:
        mock_mm.return_value = MagicMock()
        with patch("agnoclaw.agent._make_model") as mock_model:
            mock_model.return_value = MagicMock()
            build_memory_manager(extra_instructions="Remember passwords.")
            call_kwargs = mock_mm.call_args[1]
            assert "Remember passwords." in call_kwargs["additional_instructions"]


# ── build_learning_machine tests ─────────────────────────────────────────


def test_build_learning_machine_returns_object():
    from agnoclaw.memory import build_learning_machine

    with patch("agno.learn.LearningMachine") as mock_lm:
        mock_lm.return_value = MagicMock()
        with patch("agnoclaw.agent._make_model") as mock_model:
            mock_model.return_value = MagicMock()
            result = build_learning_machine()
            assert mock_lm.called


def test_build_learning_machine_default_mode_agentic():
    from agnoclaw.memory import build_learning_machine

    with patch("agno.learn.LearningMachine") as mock_lm, \
         patch("agno.learn.LearningMode") as mock_mode, \
         patch("agnoclaw.agent._make_model") as mock_model:
        mock_model.return_value = MagicMock()
        mock_lm.return_value = MagicMock()
        mock_mode.AGENTIC = "agentic"
        mock_mode.ALWAYS = "always"
        mock_mode.PROPOSE = "propose"
        mock_mode.HITL = "hitl"
        build_learning_machine()
        call_kwargs = mock_lm.call_args[1]
        assert call_kwargs["mode"] == "agentic"


def test_build_learning_machine_all_modes():
    from agnoclaw.memory import build_learning_machine

    modes = ["always", "agentic", "propose", "hitl"]

    for mode in modes:
        with patch("agno.learn.LearningMachine") as mock_lm, \
             patch("agno.learn.LearningMode") as mock_mode, \
             patch("agnoclaw.agent._make_model") as mock_model:
            mock_model.return_value = MagicMock()
            mock_lm.return_value = MagicMock()
            # Map mode strings to themselves for testing
            setattr(mock_mode, mode.upper(), mode)
            # Fallback handling
            mock_mode.AGENTIC = "agentic"
            mock_mode.ALWAYS = "always"
            mock_mode.PROPOSE = "propose"
            mock_mode.HITL = "hitl"
            build_learning_machine(mode=mode)


def test_build_learning_machine_namespace():
    from agnoclaw.memory import build_learning_machine

    with patch("agno.learn.LearningMachine") as mock_lm, \
         patch("agno.learn.LearningMode") as mock_mode, \
         patch("agnoclaw.agent._make_model") as mock_model:
        mock_model.return_value = MagicMock()
        mock_lm.return_value = MagicMock()
        mock_mode.AGENTIC = "agentic"
        build_learning_machine(namespace="research-v2")
        call_kwargs = mock_lm.call_args[1]
        assert call_kwargs["namespace"] == "research-v2"


def test_build_learning_machine_creates_db_if_none(tmp_path):
    from agnoclaw.memory import build_learning_machine

    with patch("agno.learn.LearningMachine") as mock_lm, \
         patch("agno.learn.LearningMode") as mock_mode, \
         patch("agnoclaw.agent._make_model") as mock_model, \
         patch("pathlib.Path.home", return_value=tmp_path):
        mock_model.return_value = MagicMock()
        mock_lm.return_value = MagicMock()
        mock_mode.AGENTIC = "agentic"
        build_learning_machine(db=None)
        call_kwargs = mock_lm.call_args[1]
        # Should have created a db
        assert "db" in call_kwargs


def test_build_learning_machine_uses_provided_db():
    from agnoclaw.memory import build_learning_machine

    mock_db = MagicMock()
    with patch("agno.learn.LearningMachine") as mock_lm, \
         patch("agno.learn.LearningMode") as mock_mode, \
         patch("agnoclaw.agent._make_model") as mock_model:
        mock_model.return_value = MagicMock()
        mock_lm.return_value = MagicMock()
        mock_mode.AGENTIC = "agentic"
        build_learning_machine(db=mock_db)
        call_kwargs = mock_lm.call_args[1]
        assert call_kwargs["db"] is mock_db


def test_build_learning_machine_invalid_mode_fallback_to_agentic():
    from agnoclaw.memory import build_learning_machine

    with patch("agno.learn.LearningMachine") as mock_lm, \
         patch("agno.learn.LearningMode") as mock_mode, \
         patch("agnoclaw.agent._make_model") as mock_model:
        mock_model.return_value = MagicMock()
        mock_lm.return_value = MagicMock()
        mock_mode.AGENTIC = "agentic"
        # "invalid" mode should fall back to AGENTIC
        build_learning_machine(mode="invalid_mode")
        call_kwargs = mock_lm.call_args[1]
        assert call_kwargs["mode"] == "agentic"


# ── No CultureManager tests ───────────────────────────────────────────────


def test_no_culture_manager_in_memory_module():
    """CultureManager should NOT exist in the memory module."""
    import agnoclaw.memory as mem
    assert not hasattr(mem, "build_culture_manager"), (
        "build_culture_manager should have been removed"
    )
