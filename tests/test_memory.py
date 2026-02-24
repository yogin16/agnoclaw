"""Tests for the memory management utilities."""

from unittest.mock import MagicMock, patch


# ── build_memory_manager tests ───────────────────────────────────────────


def test_build_memory_manager_returns_object():
    """build_memory_manager should return a MemoryManager without error."""
    from agnoclaw.memory import build_memory_manager

    with patch("agno.memory.MemoryManager") as mock_mm:
        mock_mm.return_value = MagicMock()
        with patch("agnoclaw.agent._resolve_model", return_value="anthropic:claude-sonnet-4-6") as mock_model:
            mock_model.return_value = MagicMock()
            build_memory_manager()
            assert mock_mm.called


def test_build_memory_manager_uses_haiku_by_default():
    from agnoclaw.memory import build_memory_manager

    with patch("agno.memory.MemoryManager"):
        with patch("agnoclaw.agent._resolve_model", return_value="anthropic:claude-sonnet-4-6") as mock_model:
            mock_model.return_value = MagicMock()
            build_memory_manager()
            args = mock_model.call_args[0]; assert args[0] == "claude-haiku-4-5-20251001" and args[1] == "anthropic"


def test_build_memory_manager_accepts_custom_model():
    from agnoclaw.memory import build_memory_manager

    with patch("agno.memory.MemoryManager"):
        with patch("agnoclaw.agent._resolve_model", return_value="anthropic:claude-sonnet-4-6") as mock_model:
            mock_model.return_value = MagicMock()
            build_memory_manager(model_id="gpt-4o-mini", provider="openai")
            args = mock_model.call_args[0]; assert args[0] == "gpt-4o-mini" and args[1] == "openai"


def test_build_memory_manager_passes_db():
    from agnoclaw.memory import build_memory_manager

    mock_db = MagicMock()

    with patch("agno.memory.MemoryManager") as mock_mm:
        mock_mm.return_value = MagicMock()
        with patch("agnoclaw.agent._resolve_model", return_value="anthropic:claude-sonnet-4-6") as mock_model:
            mock_model.return_value = MagicMock()
            build_memory_manager(db=mock_db)
            call_kwargs = mock_mm.call_args[1]
            assert call_kwargs.get("db") is mock_db


def test_build_memory_manager_no_db_kwarg_without_db():
    """When db=None, 'db' key should not be passed to MemoryManager."""
    from agnoclaw.memory import build_memory_manager

    with patch("agno.memory.MemoryManager") as mock_mm:
        mock_mm.return_value = MagicMock()
        with patch("agnoclaw.agent._resolve_model", return_value="anthropic:claude-sonnet-4-6") as mock_model:
            mock_model.return_value = MagicMock()
            build_memory_manager(db=None)
            call_kwargs = mock_mm.call_args[1]
            assert "db" not in call_kwargs


def test_build_memory_manager_extra_instructions():
    from agnoclaw.memory import build_memory_manager

    with patch("agno.memory.MemoryManager") as mock_mm:
        mock_mm.return_value = MagicMock()
        with patch("agnoclaw.agent._resolve_model", return_value="anthropic:claude-sonnet-4-6") as mock_model:
            mock_model.return_value = MagicMock()
            build_memory_manager(extra_instructions="Remember passwords.")
            call_kwargs = mock_mm.call_args[1]
            assert "Remember passwords." in call_kwargs["additional_instructions"]


# ── build_learning_machine tests ─────────────────────────────────────────


def _lm_patches():
    """Return patch.multiple context for all LearningMachine dependencies."""
    return patch.multiple(
        "agnoclaw.memory",
        **{},
    )


def test_build_learning_machine_returns_object():
    from agnoclaw.memory import build_learning_machine

    with patch("agno.learn.LearningMachine") as mock_lm, \
         patch("agno.learn.LearningMode") as mock_mode, \
         patch("agno.learn.config.EntityMemoryConfig"), \
         patch("agno.learn.config.LearnedKnowledgeConfig"), \
         patch("agno.learn.config.DecisionLogConfig"), \
         patch("agnoclaw.agent._resolve_model", return_value="anthropic:claude-sonnet-4-6") as mock_model:
        mock_model.return_value = MagicMock()
        mock_lm.return_value = MagicMock()
        mock_mode.AGENTIC = "agentic"
        mock_mode.ALWAYS = "always"
        build_learning_machine()
        assert mock_lm.called


def test_build_learning_machine_no_top_level_mode():
    """LearningMachine should NOT receive a top-level mode= param.

    Mode is configured per-store via EntityMemoryConfig, DecisionLogConfig, etc.
    Passing mode= at the top level to LearningMachine would raise a TypeError.
    """
    from agnoclaw.memory import build_learning_machine

    with patch("agno.learn.LearningMachine") as mock_lm, \
         patch("agno.learn.LearningMode") as mock_mode, \
         patch("agno.learn.config.EntityMemoryConfig"), \
         patch("agno.learn.config.LearnedKnowledgeConfig"), \
         patch("agno.learn.config.DecisionLogConfig"), \
         patch("agnoclaw.agent._resolve_model", return_value="anthropic:claude-sonnet-4-6") as mock_model:
        mock_model.return_value = MagicMock()
        mock_lm.return_value = MagicMock()
        mock_mode.AGENTIC = "agentic"
        mock_mode.ALWAYS = "always"
        build_learning_machine()
        call_kwargs = mock_lm.call_args[1]
        assert "mode" not in call_kwargs, (
            "LearningMachine should not receive top-level mode=; use per-store configs"
        )


def test_build_learning_machine_excludes_per_user_stores():
    """user_profile and user_memory should be disabled — use MemoryManager instead."""
    from agnoclaw.memory import build_learning_machine

    with patch("agno.learn.LearningMachine") as mock_lm, \
         patch("agno.learn.LearningMode") as mock_mode, \
         patch("agno.learn.config.EntityMemoryConfig"), \
         patch("agno.learn.config.LearnedKnowledgeConfig"), \
         patch("agno.learn.config.DecisionLogConfig"), \
         patch("agnoclaw.agent._resolve_model", return_value="anthropic:claude-sonnet-4-6") as mock_model:
        mock_model.return_value = MagicMock()
        mock_lm.return_value = MagicMock()
        mock_mode.AGENTIC = "agentic"
        mock_mode.ALWAYS = "always"
        build_learning_machine()
        call_kwargs = mock_lm.call_args[1]
        assert call_kwargs.get("user_profile") is False, "user_profile should be False"
        assert call_kwargs.get("user_memory") is False, "user_memory should be False"


def test_build_learning_machine_has_per_store_configs():
    """entity_memory, learned_knowledge, decision_log should be configured."""
    from agnoclaw.memory import build_learning_machine

    with patch("agno.learn.LearningMachine") as mock_lm, \
         patch("agno.learn.LearningMode") as mock_mode, \
         patch("agno.learn.config.EntityMemoryConfig") as mock_emc, \
         patch("agno.learn.config.LearnedKnowledgeConfig"), \
         patch("agno.learn.config.DecisionLogConfig"), \
         patch("agnoclaw.agent._resolve_model", return_value="anthropic:claude-sonnet-4-6") as mock_model:
        mock_model.return_value = MagicMock()
        mock_lm.return_value = MagicMock()
        mock_mode.AGENTIC = "agentic"
        mock_mode.ALWAYS = "always"
        mock_emc.return_value = MagicMock(name="entity_config")
        build_learning_machine()
        call_kwargs = mock_lm.call_args[1]
        assert "entity_memory" in call_kwargs and call_kwargs["entity_memory"] is not None
        assert "learned_knowledge" in call_kwargs and call_kwargs["learned_knowledge"] is not None
        assert "decision_log" in call_kwargs and call_kwargs["decision_log"] is not None


def test_build_learning_machine_session_context_disabled_by_default():
    """session_context should NOT be present by default."""
    from agnoclaw.memory import build_learning_machine

    with patch("agno.learn.LearningMachine") as mock_lm, \
         patch("agno.learn.LearningMode") as mock_mode, \
         patch("agno.learn.config.EntityMemoryConfig"), \
         patch("agno.learn.config.LearnedKnowledgeConfig"), \
         patch("agno.learn.config.DecisionLogConfig"), \
         patch("agnoclaw.agent._resolve_model", return_value="anthropic:claude-sonnet-4-6") as mock_model:
        mock_model.return_value = MagicMock()
        mock_lm.return_value = MagicMock()
        mock_mode.AGENTIC = "agentic"
        mock_mode.ALWAYS = "always"
        build_learning_machine()
        call_kwargs = mock_lm.call_args[1]
        assert "session_context" not in call_kwargs


def test_build_learning_machine_session_context_opt_in():
    """enable_session_context=True should add session_context store."""
    from agnoclaw.memory import build_learning_machine

    with patch("agno.learn.LearningMachine") as mock_lm, \
         patch("agno.learn.LearningMode") as mock_mode, \
         patch("agno.learn.config.EntityMemoryConfig"), \
         patch("agno.learn.config.LearnedKnowledgeConfig"), \
         patch("agno.learn.config.DecisionLogConfig"), \
         patch("agno.learn.config.SessionContextConfig") as mock_scc, \
         patch("agnoclaw.agent._resolve_model", return_value="anthropic:claude-sonnet-4-6") as mock_model:
        mock_model.return_value = MagicMock()
        mock_lm.return_value = MagicMock()
        mock_mode.AGENTIC = "agentic"
        mock_mode.ALWAYS = "always"
        mock_scc.return_value = MagicMock(name="session_config")
        build_learning_machine(enable_session_context=True)
        call_kwargs = mock_lm.call_args[1]
        assert "session_context" in call_kwargs and call_kwargs["session_context"] is not None


def test_build_learning_machine_entity_memory_mode_propagated():
    """The mode string should be propagated to EntityMemoryConfig."""
    from agnoclaw.memory import build_learning_machine

    with patch("agno.learn.LearningMachine") as mock_lm, \
         patch("agno.learn.LearningMode") as mock_mode, \
         patch("agno.learn.config.EntityMemoryConfig") as mock_emc, \
         patch("agno.learn.config.LearnedKnowledgeConfig"), \
         patch("agno.learn.config.DecisionLogConfig"), \
         patch("agnoclaw.agent._resolve_model", return_value="anthropic:claude-sonnet-4-6") as mock_model:
        mock_model.return_value = MagicMock()
        mock_lm.return_value = MagicMock()
        mock_mode.AGENTIC = "agentic"
        mock_mode.ALWAYS = "always"
        mock_mode.PROPOSE = "propose"
        mock_mode.HITL = "hitl"
        build_learning_machine(mode="always")
        # EntityMemoryConfig should have been called with mode=ALWAYS
        emc_kwargs = mock_emc.call_args[1]
        assert emc_kwargs["mode"] == "always"


def test_build_learning_machine_learned_knowledge_always_agentic():
    """learned_knowledge should always use AGENTIC mode regardless of mode param."""
    from agnoclaw.memory import build_learning_machine

    with patch("agno.learn.LearningMachine") as mock_lm, \
         patch("agno.learn.LearningMode") as mock_mode, \
         patch("agno.learn.config.EntityMemoryConfig"), \
         patch("agno.learn.config.LearnedKnowledgeConfig") as mock_lkc, \
         patch("agno.learn.config.DecisionLogConfig"), \
         patch("agnoclaw.agent._resolve_model", return_value="anthropic:claude-sonnet-4-6") as mock_model:
        mock_model.return_value = MagicMock()
        mock_lm.return_value = MagicMock()
        mock_mode.AGENTIC = "agentic"
        mock_mode.ALWAYS = "always"
        build_learning_machine(mode="always")  # even when mode=always is requested
        lkc_kwargs = mock_lkc.call_args[1]
        # learned_knowledge always uses AGENTIC
        assert lkc_kwargs["mode"] == "agentic"


def test_build_learning_machine_all_modes_accepted():
    """All four mode strings should be accepted without error."""
    from agnoclaw.memory import build_learning_machine

    for mode in ["always", "agentic", "propose", "hitl"]:
        with patch("agno.learn.LearningMachine") as mock_lm, \
             patch("agno.learn.LearningMode") as mock_mode, \
             patch("agno.learn.config.EntityMemoryConfig"), \
             patch("agno.learn.config.LearnedKnowledgeConfig"), \
             patch("agno.learn.config.DecisionLogConfig"), \
             patch("agnoclaw.agent._resolve_model", return_value="anthropic:claude-sonnet-4-6") as mock_model:
            mock_model.return_value = MagicMock()
            mock_lm.return_value = MagicMock()
            mock_mode.AGENTIC = "agentic"
            mock_mode.ALWAYS = "always"
            mock_mode.PROPOSE = "propose"
            mock_mode.HITL = "hitl"
            build_learning_machine(mode=mode)
            assert mock_lm.called, f"LearningMachine not called for mode={mode}"


def test_build_learning_machine_invalid_mode_fallback_to_agentic():
    """Unknown mode string should fall back to AGENTIC."""
    from agnoclaw.memory import build_learning_machine

    with patch("agno.learn.LearningMachine") as mock_lm, \
         patch("agno.learn.LearningMode") as mock_mode, \
         patch("agno.learn.config.EntityMemoryConfig") as mock_emc, \
         patch("agno.learn.config.LearnedKnowledgeConfig"), \
         patch("agno.learn.config.DecisionLogConfig"), \
         patch("agnoclaw.agent._resolve_model", return_value="anthropic:claude-sonnet-4-6") as mock_model:
        mock_model.return_value = MagicMock()
        mock_lm.return_value = MagicMock()
        mock_mode.AGENTIC = "agentic"
        mock_mode.ALWAYS = "always"
        build_learning_machine(mode="invalid_mode")
        emc_kwargs = mock_emc.call_args[1]
        # Falls back to AGENTIC
        assert emc_kwargs["mode"] == "agentic"


def test_build_learning_machine_namespace():
    from agnoclaw.memory import build_learning_machine

    with patch("agno.learn.LearningMachine") as mock_lm, \
         patch("agno.learn.LearningMode") as mock_mode, \
         patch("agno.learn.config.EntityMemoryConfig"), \
         patch("agno.learn.config.LearnedKnowledgeConfig"), \
         patch("agno.learn.config.DecisionLogConfig"), \
         patch("agnoclaw.agent._resolve_model", return_value="anthropic:claude-sonnet-4-6") as mock_model:
        mock_model.return_value = MagicMock()
        mock_lm.return_value = MagicMock()
        mock_mode.AGENTIC = "agentic"
        mock_mode.ALWAYS = "always"
        build_learning_machine(namespace="research-v2")
        call_kwargs = mock_lm.call_args[1]
        assert call_kwargs["namespace"] == "research-v2"


def test_build_learning_machine_default_namespace_global():
    """When namespace=None, should use 'global' namespace."""
    from agnoclaw.memory import build_learning_machine

    with patch("agno.learn.LearningMachine") as mock_lm, \
         patch("agno.learn.LearningMode") as mock_mode, \
         patch("agno.learn.config.EntityMemoryConfig"), \
         patch("agno.learn.config.LearnedKnowledgeConfig"), \
         patch("agno.learn.config.DecisionLogConfig"), \
         patch("agnoclaw.agent._resolve_model", return_value="anthropic:claude-sonnet-4-6") as mock_model:
        mock_model.return_value = MagicMock()
        mock_lm.return_value = MagicMock()
        mock_mode.AGENTIC = "agentic"
        mock_mode.ALWAYS = "always"
        build_learning_machine(namespace=None)
        call_kwargs = mock_lm.call_args[1]
        assert call_kwargs["namespace"] == "global"


def test_build_learning_machine_creates_db_if_none(tmp_path):
    from agnoclaw.memory import build_learning_machine

    with patch("agno.learn.LearningMachine") as mock_lm, \
         patch("agno.learn.LearningMode") as mock_mode, \
         patch("agno.learn.config.EntityMemoryConfig"), \
         patch("agno.learn.config.LearnedKnowledgeConfig"), \
         patch("agno.learn.config.DecisionLogConfig"), \
         patch("agnoclaw.agent._resolve_model", return_value="anthropic:claude-sonnet-4-6") as mock_model, \
         patch("pathlib.Path.home", return_value=tmp_path):
        mock_model.return_value = MagicMock()
        mock_lm.return_value = MagicMock()
        mock_mode.AGENTIC = "agentic"
        mock_mode.ALWAYS = "always"
        build_learning_machine(db=None)
        call_kwargs = mock_lm.call_args[1]
        # Should have created a db
        assert "db" in call_kwargs


def test_build_learning_machine_uses_provided_db():
    from agnoclaw.memory import build_learning_machine

    mock_db = MagicMock()
    with patch("agno.learn.LearningMachine") as mock_lm, \
         patch("agno.learn.LearningMode") as mock_mode, \
         patch("agno.learn.config.EntityMemoryConfig"), \
         patch("agno.learn.config.LearnedKnowledgeConfig"), \
         patch("agno.learn.config.DecisionLogConfig"), \
         patch("agnoclaw.agent._resolve_model", return_value="anthropic:claude-sonnet-4-6") as mock_model:
        mock_model.return_value = MagicMock()
        mock_lm.return_value = MagicMock()
        mock_mode.AGENTIC = "agentic"
        mock_mode.ALWAYS = "always"
        build_learning_machine(db=mock_db)
        call_kwargs = mock_lm.call_args[1]
        assert call_kwargs["db"] is mock_db


# ── No CultureManager tests ───────────────────────────────────────────────


def test_no_culture_manager_in_memory_module():
    """CultureManager should NOT exist in the memory module."""
    import agnoclaw.memory as mem
    assert not hasattr(mem, "build_culture_manager"), (
        "build_culture_manager should have been removed"
    )


# ── enable_user_memory tests ──────────────────────────────────────────────


def test_build_learning_machine_user_memory_enabled():
    """enable_user_memory=True should set user_profile and user_memory to True."""
    from agnoclaw.memory import build_learning_machine

    with patch("agno.learn.LearningMachine") as mock_lm, \
         patch("agno.learn.LearningMode") as mock_mode, \
         patch("agno.learn.config.EntityMemoryConfig"), \
         patch("agno.learn.config.LearnedKnowledgeConfig"), \
         patch("agno.learn.config.DecisionLogConfig"), \
         patch("agnoclaw.agent._resolve_model", return_value="anthropic:claude-sonnet-4-6") as mock_model:
        mock_model.return_value = MagicMock()
        mock_lm.return_value = MagicMock()
        mock_mode.AGENTIC = "agentic"
        mock_mode.ALWAYS = "always"
        build_learning_machine(enable_user_memory=True)
        call_kwargs = mock_lm.call_args[1]
        assert call_kwargs.get("user_profile") is True
        assert call_kwargs.get("user_memory") is True


def test_build_learning_machine_user_memory_disabled_by_default():
    """enable_user_memory defaults to False — user stores should be False."""
    from agnoclaw.memory import build_learning_machine

    with patch("agno.learn.LearningMachine") as mock_lm, \
         patch("agno.learn.LearningMode") as mock_mode, \
         patch("agno.learn.config.EntityMemoryConfig"), \
         patch("agno.learn.config.LearnedKnowledgeConfig"), \
         patch("agno.learn.config.DecisionLogConfig"), \
         patch("agnoclaw.agent._resolve_model", return_value="anthropic:claude-sonnet-4-6") as mock_model:
        mock_model.return_value = MagicMock()
        mock_lm.return_value = MagicMock()
        mock_mode.AGENTIC = "agentic"
        mock_mode.ALWAYS = "always"
        build_learning_machine()
        call_kwargs = mock_lm.call_args[1]
        assert call_kwargs.get("user_profile") is False
        assert call_kwargs.get("user_memory") is False


def test_build_memory_manager_deprecation_warning():
    """build_memory_manager() should emit a DeprecationWarning."""
    import warnings
    from agnoclaw.memory import build_memory_manager

    with patch("agno.memory.MemoryManager"), \
         patch("agnoclaw.agent._resolve_model", return_value="anthropic:claude-sonnet-4-6") as mock_model:
        mock_model.return_value = MagicMock()
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            build_memory_manager()
            assert len(w) == 1
            assert issubclass(w[0].category, DeprecationWarning)
            assert "deprecated" in str(w[0].message).lower()
