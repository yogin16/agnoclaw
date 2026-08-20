"""Evidence-bound long-run continuation extraction contracts."""

from __future__ import annotations

import json
from types import SimpleNamespace

from agnoclaw.context_management import ContextContinuationRecord
from agnoclaw.context_runtime import _ContextManagementMixin


def _message(role: str, content: str, **extra):
    return SimpleNamespace(role=role, content=content, **extra)


def test_verified_continuation_accepts_only_exact_source_spans() -> None:
    messages = [
        _message("user", "Build a durable agent harness."),
        _message(
            "assistant",
            "Plan: implement verified checkpoint extraction.\n"
            "Decision: keep approvals evidence-bound.",
        ),
        _message(
            "tool",
            "Tests: 42 focused tests passed.\n"
            "File: src/agnoclaw/context_runtime.py",
        ),
    ]
    sources = _ContextManagementMixin._continuation_extraction_sources(messages)
    raw = json.dumps(
        {
            "summary": "Checkpoint extraction is implemented and verified.",
            "entries": [
                {
                    "field": "plan",
                    "source_ordinal": 1,
                    "exact_text": "Plan: implement verified checkpoint extraction.",
                },
                {
                    "field": "decisions",
                    "source_ordinal": 1,
                    "exact_text": "Decision: keep approvals evidence-bound.",
                },
                {
                    "field": "tests",
                    "source_ordinal": 2,
                    "exact_text": "Tests: 42 focused tests passed.",
                },
                {
                    "field": "files",
                    "source_ordinal": 2,
                    "exact_text": "File: src/agnoclaw/context_runtime.py",
                },
                {
                    "field": "approvals",
                    "source_ordinal": 0,
                    "exact_text": "The operator approved deployment.",
                },
            ],
        }
    )

    verified = _ContextManagementMixin._verified_continuation_proposal(
        raw,
        sources=sources,
        carried=None,
        carried_sources={},
        capture_initial_goal=True,
        manifest_revision=0,
        preflush_messages=messages,
    )

    assert verified is not None
    summary, record, provenance = verified
    assert summary == "Checkpoint extraction is implemented and verified."
    assert record is not None
    assert record.goal == "Build a durable agent harness."
    assert record.plan == ("Plan: implement verified checkpoint extraction.",)
    assert record.decisions == ("Decision: keep approvals evidence-bound.",)
    assert record.tests == ("Tests: 42 focused tests passed.",)
    assert record.files == ("File: src/agnoclaw/context_runtime.py",)
    assert record.approvals == ()
    assert provenance[("goal", 0)]["extraction"] == "deterministic_initial_goal_v1"
    assert provenance[("plan", 0)]["extraction"] == "model_proposed_exact_span_v1"
    assert provenance[("plan", 0)]["source_ordinal"] == 1


def test_verified_continuation_replaces_live_state_and_unions_durable_history() -> None:
    messages = [
        _message("user", "Continue the harness release."),
        _message(
            "assistant",
            "Current plan: finish continuation fidelity.\n"
            "Decision: defer packaging until product behavior is complete.",
        ),
    ]
    carried = ContextContinuationRecord(
        summary="Previous checkpoint.",
        goal="Build a world-class harness.",
        plan=("Old plan: package the release.",),
        progress=("Skill activation landed.",),
        decisions=("Decision: use Agno-native skills.",),
        open_questions=("How should checkpoints merge?",),
        tests=("209 focused tests passed.",),
    )
    raw = json.dumps(
        {
            "summary": "Continuation fidelity is now the active product slice.",
            "entries": [
                {
                    "field": "plan",
                    "source_ordinal": 1,
                    "exact_text": "Current plan: finish continuation fidelity.",
                },
                {
                    "field": "decisions",
                    "source_ordinal": 1,
                    "exact_text": (
                        "Decision: defer packaging until product behavior is complete."
                    ),
                },
            ],
        }
    )

    verified = _ContextManagementMixin._verified_continuation_proposal(
        raw,
        sources=_ContextManagementMixin._continuation_extraction_sources(messages),
        carried=carried,
        carried_sources={("decisions", 0): {"source_item_id": "old-decision"}},
        capture_initial_goal=True,
        manifest_revision=2,
        preflush_messages=messages,
    )

    assert verified is not None
    _summary, record, provenance = verified
    assert record is not None
    assert record.goal == "Build a world-class harness."
    assert record.plan == ("Current plan: finish continuation fidelity.",)
    assert record.progress == ("Skill activation landed.",)
    assert record.open_questions == ("How should checkpoints merge?",)
    assert record.tests == ("209 focused tests passed.",)
    assert record.decisions == (
        "Decision: use Agno-native skills.",
        "Decision: defer packaging until product behavior is complete.",
    )
    assert provenance[("decisions", 0)]["source_item_id"] == "old-decision"
    assert provenance[("decisions", 1)]["source_ordinal"] == 1


def test_verified_continuation_rejects_invalid_envelopes() -> None:
    messages = [_message("user", "Keep working.")]
    sources = _ContextManagementMixin._continuation_extraction_sources(messages)

    assert (
        _ContextManagementMixin._verified_continuation_proposal(
            "not-json",
            sources=sources,
            carried=None,
            carried_sources={},
            capture_initial_goal=True,
            manifest_revision=0,
            preflush_messages=messages,
        )
        is None
    )
