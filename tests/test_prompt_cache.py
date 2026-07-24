"""Tests for prompt-cache friendliness: the stable/runtime system-prompt
split and the Anthropic conversation-prefix cache annotator."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from agnoclaw.prompts.system import SystemPromptBuilder


@pytest.fixture
def builder(tmp_path):
    return SystemPromptBuilder(tmp_path)


# ── Stable prompt / runtime block split ──────────────────────────────────


def test_stable_prompt_byte_identical_across_clock_changes(builder):
    """The cache regression guard: same inputs, different wall-clock
    seconds → byte-identical stable content."""
    fake_dt = MagicMock(wraps=datetime)
    fake_dt.now.return_value = datetime(2026, 7, 24, 10, 15, 30)
    with patch("agnoclaw.prompts.system.datetime", fake_dt):
        first = builder.build(include_datetime=False, session_id="cs_a")
    fake_dt.now.return_value = datetime(2026, 7, 24, 10, 15, 55)
    with patch("agnoclaw.prompts.system.datetime", fake_dt):
        second = builder.build(include_datetime=False, session_id="cs_a")
    assert first == second
    assert "# Runtime" not in first


def test_full_build_stable_within_a_day(builder):
    """Even the joined prompt (runtime inline) is stable across
    same-day rebuilds now that the timestamp is date-only."""
    fake_dt = MagicMock(wraps=datetime)
    fake_dt.now.return_value = datetime(2026, 7, 24, 9, 0, 1)
    with patch("agnoclaw.prompts.system.datetime", fake_dt):
        first = builder.build(session_id="cs_a")
    fake_dt.now.return_value = datetime(2026, 7, 24, 23, 59, 59)
    with patch("agnoclaw.prompts.system.datetime", fake_dt):
        second = builder.build(session_id="cs_a")
    assert first == second


def test_runtime_block_is_date_only_and_carries_session_id(builder):
    fake_dt = MagicMock(wraps=datetime)
    fake_dt.now.return_value = datetime(2026, 7, 24, 10, 15, 30)
    with patch("agnoclaw.prompts.system.datetime", fake_dt):
        block = builder.build_runtime_block(session_id="cs_deadbeef")
    assert block.startswith("# Runtime")
    assert "2026-07-24" in block
    assert "10:15" not in block
    assert "Session ID: cs_deadbeef" in block


def test_build_still_appends_runtime_by_default(builder):
    full = builder.build(session_id="cs_a")
    assert "# Runtime" in full
    assert "Session ID: cs_a" in full


# ── Model instance passthrough ───────────────────────────────────────────


def test_resolve_model_passes_instances_through():
    from agnoclaw.agent import _resolve_model
    from agnoclaw.config import HarnessConfig

    sentinel = object.__new__(type("FakeModel", (), {}))
    assert _resolve_model(sentinel, None, HarnessConfig()) is sentinel


def test_resolve_model_string_behavior_unchanged():
    from agnoclaw.agent import _resolve_model
    from agnoclaw.config import HarnessConfig

    assert (
        _resolve_model("anthropic:claude-sonnet-4-6", None, HarnessConfig())
        == "anthropic:claude-sonnet-4-6"
    )


# ── Split-prompt cache mode on the harness ───────────────────────────────


def _make_harness(tmp_path, model):
    from agnoclaw.agent import AgentHarness
    from agnoclaw.config import HarnessConfig

    agent_cls = MagicMock(return_value=MagicMock())
    with patch("agnoclaw.agent.Agent", agent_cls):
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            harness = AgentHarness(
                model=model,
                workspace_dir=tmp_path,
                config=HarnessConfig(),
                session_id="cs_split",
            )
    return harness, agent_cls


def test_split_mode_active_for_cache_enabled_claude_instance(tmp_path):
    from agnoclaw.models.anthropic import CacheAwareClaude

    model = CacheAwareClaude(
        id="claude-sonnet-4-6", cache_system_prompt=True, cache_tools=True
    )
    harness, agent_cls = _make_harness(tmp_path, model)

    system_message = agent_cls.call_args.kwargs["system_message"]
    assert "# Runtime" not in system_message
    assert callable(model.system_prompt_blocks)
    blocks = model.system_prompt_blocks()
    assert len(blocks) == 1
    assert blocks[0].cache is False
    assert blocks[0].text.startswith("# Runtime")
    assert "Session ID: cs_split" in blocks[0].text


def test_split_mode_survives_prompt_rebuild(tmp_path):
    from agnoclaw.models.anthropic import CacheAwareClaude

    model = CacheAwareClaude(id="claude-sonnet-4-6", cache_system_prompt=True)
    harness, _ = _make_harness(tmp_path, model)
    harness._set_system_prompt(skill_content="# Skill\n\ndo the thing")
    assert "# Runtime" not in harness._agent.system_message
    assert "do the thing" in harness._agent.system_message
    assert callable(model.system_prompt_blocks)


def test_string_model_keeps_inline_runtime(tmp_path):
    harness, agent_cls = _make_harness(tmp_path, "anthropic:claude-sonnet-4-6")
    system_message = agent_cls.call_args.kwargs["system_message"]
    assert "# Runtime" in system_message
    assert "Session ID: cs_split" in system_message


def test_uncached_claude_instance_keeps_inline_runtime(tmp_path):
    from agno.models.anthropic import Claude

    model = Claude(id="claude-sonnet-4-6")  # cache_system_prompt defaults False
    harness, agent_cls = _make_harness(tmp_path, model)
    system_message = agent_cls.call_args.kwargs["system_message"]
    assert "# Runtime" in system_message
    assert model.system_prompt_blocks is None


# ── Conversation-prefix breakpoint annotator ─────────────────────────────


def _tool_turn_blocks(n):
    return [
        {"type": "tool_result", "tool_use_id": f"toolu_{i}", "content": "ok"}
        for i in range(n)
    ]


def test_annotate_tags_last_block():
    from agnoclaw.models.anthropic import annotate_conversation_breakpoints

    messages = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "a"},
                {"type": "text", "text": "b"},
            ],
        },
    ]
    annotate_conversation_breakpoints(messages)
    assert messages[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in messages[-1]["content"][0]
    assert "cache_control" not in messages[0]["content"][0]


def test_annotate_promotes_string_content():
    from agnoclaw.models.anthropic import annotate_conversation_breakpoints

    messages = [{"role": "user", "content": "score this deck"}]
    annotate_conversation_breakpoints(messages)
    content = messages[0]["content"]
    assert isinstance(content, list)
    assert content[0]["text"] == "score this deck"
    assert content[0]["cache_control"] == {"type": "ephemeral"}


def test_annotate_intermediate_breakpoint_for_long_turns():
    """>15 blocks since the trailing breakpoint → a second breakpoint
    lands within the 20-block lookback window."""
    from agnoclaw.models.anthropic import annotate_conversation_breakpoints

    messages = [
        {"role": "user", "content": [{"type": "text", "text": "start"}]},
        {"role": "user", "content": _tool_turn_blocks(30)},
    ]
    annotate_conversation_breakpoints(messages)
    blocks = messages[1]["content"]
    tagged = [i for i, b in enumerate(blocks) if "cache_control" in b]
    # Trailing breakpoint on the last block plus one intermediate
    assert tagged == [13, 29]
    assert "cache_control" not in messages[0]["content"][0]


def test_annotate_caps_at_two_breakpoints():
    from agnoclaw.models.anthropic import annotate_conversation_breakpoints

    messages = [{"role": "user", "content": _tool_turn_blocks(100)}]
    annotate_conversation_breakpoints(messages)
    tagged = [b for b in messages[0]["content"] if "cache_control" in b]
    assert len(tagged) == 2


def test_annotate_skips_thinking_blocks():
    from agnoclaw.models.anthropic import annotate_conversation_breakpoints

    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "final answer"},
                {"type": "thinking", "thinking": "...", "signature": "sig"},
            ],
        }
    ]
    annotate_conversation_breakpoints(messages)
    assert "cache_control" not in messages[0]["content"][1]
    assert messages[0]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_annotate_noop_on_empty_input():
    from agnoclaw.models.anthropic import annotate_conversation_breakpoints

    annotate_conversation_breakpoints([])
    annotate_conversation_breakpoints(None)
    messages = [{"role": "user", "content": ""}]
    annotate_conversation_breakpoints(messages)
    assert messages[0]["content"] == ""


# ── CacheAwareClaude client proxy ────────────────────────────────────────


def test_client_proxy_annotates_create_and_stream():
    from agnoclaw.models.anthropic import _CachingClientProxy

    inner = MagicMock()
    proxy = _CachingClientProxy(inner)
    messages = [{"role": "user", "content": "hi"}]
    proxy.messages.create(model="claude-sonnet-4-6", messages=messages)
    assert messages[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    inner.messages.create.assert_called_once()

    beta_messages = [{"role": "user", "content": "hello"}]
    proxy.beta.messages.stream(model="claude-sonnet-4-6", messages=beta_messages)
    assert beta_messages[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    inner.beta.messages.stream.assert_called_once()


def test_cache_aware_claude_wraps_clients():
    from agnoclaw.models.anthropic import CacheAwareClaude, _CachingClientProxy

    model = CacheAwareClaude(id="claude-sonnet-4-6", api_key="test-key")
    assert isinstance(model.get_client(), _CachingClientProxy)
    assert isinstance(model.get_async_client(), _CachingClientProxy)

    plain = CacheAwareClaude(
        id="claude-sonnet-4-6", api_key="test-key", cache_conversation=False
    )
    assert not isinstance(plain.get_client(), _CachingClientProxy)


# ── materialize_model provider dispatch ──────────────────────────────────


def test_materialize_model_passthrough_for_sentinels_and_unknown_providers():
    from agnoclaw.models import materialize_model

    assert materialize_model("mock", cache_prompts=True) == "mock"
    assert (
        materialize_model("replay:/tmp/run.jsonl", cache_prompts=True)
        == "replay:/tmp/run.jsonl"
    )
    assert (
        materialize_model("openai:gpt-4o", cache_prompts=True) == "openai:gpt-4o"
    )
    sentinel = object()
    assert materialize_model(sentinel, cache_prompts=True) is sentinel


def test_materialize_model_builds_cache_aware_claude():
    from agnoclaw.models import materialize_model
    from agnoclaw.models.anthropic import CacheAwareClaude

    model = materialize_model(
        "anthropic:claude-sonnet-4-6", cache_prompts=True, effort="medium"
    )
    assert isinstance(model, CacheAwareClaude)
    assert model.id == "claude-sonnet-4-6"
    assert model.cache_system_prompt is True
    assert model.cache_tools is True
    assert model.cache_conversation is True
    assert model.output_config == {"effort": "medium"}


def test_materialize_model_drops_effort_on_unsupporting_models():
    from agnoclaw.models import materialize_model

    model = materialize_model(
        "anthropic:claude-haiku-4-5", cache_prompts=True, effort="low"
    )
    assert model.output_config is None
    assert model.cache_system_prompt is True


def test_materialize_model_no_options_returns_spec_string():
    from agnoclaw.models import materialize_model

    assert (
        materialize_model("anthropic:claude-sonnet-4-6", cache_prompts=False)
        == "anthropic:claude-sonnet-4-6"
    )


def test_materialize_model_caching_off_effort_still_builds_plain_claude():
    from agno.models.anthropic import Claude

    from agnoclaw.models import materialize_model
    from agnoclaw.models.anthropic import CacheAwareClaude

    model = materialize_model(
        "anthropic:claude-sonnet-4-6", cache_prompts=False, effort="low"
    )
    assert isinstance(model, Claude)
    assert not isinstance(model, CacheAwareClaude)
    assert model.output_config == {"effort": "low"}


def test_materialize_model_applies_claude_37_beta():
    from agnoclaw.models import materialize_model

    model = materialize_model(
        "anthropic:claude-3-7-sonnet-20250219", cache_prompts=True
    )
    assert model.betas == ["token-efficient-tools-2025-02-19"]
    # 3.x also predates effort support
    assert model.output_config is None
