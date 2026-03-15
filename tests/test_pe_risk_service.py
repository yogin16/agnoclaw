"""Tests for PE risk review service wrapper."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from examples.pe_risk_platform.service import PERiskReviewService


def test_service_quality_gate_fails_for_sparse_attachment(tmp_path):
    workspace = tmp_path / "workspace"
    note = tmp_path / "note.txt"
    note.write_text("Only narrative text. No underwriting metrics.", encoding="utf-8")

    service = PERiskReviewService(
        model="anthropic:claude-sonnet-4-6",
        workspace_dir=workspace,
        min_completeness=0.85,
    )

    result = service.review(
        user_message="Please assess this deal.",
        attachments=[note],
        deal_id="PE-REAL-SPARSE-01",
        deal_name="Sparse Deal",
        sector="Unknown",
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "QUALITY_GATE_FAILED"


def test_service_review_success_with_mocked_harness(tmp_path):
    workspace = tmp_path / "workspace"
    deal_fixture = Path(
        "examples/pe_risk_platform/fixtures/deals/PE-2026-001-northstar-vertical-software.json"
    )

    class FakeHarness:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        def run(self, prompt, *, skill=None, context=None):
            del prompt, context
            if skill == "pe-due-diligence-lifecycle":
                return SimpleNamespace(
                    content=("Risk Register\nOpen Diligence Questions\nGo / No-go Recommendation\n")
                )
            return SimpleNamespace(
                content=("Composite Risk Score\nTop Score Drivers\nInvestment Committee Action\n")
            )

    with patch("examples.pe_risk_platform.service.AgentHarness", FakeHarness):
        service = PERiskReviewService(
            model="anthropic:claude-sonnet-4-6",
            workspace_dir=workspace,
            min_completeness=0.85,
        )
        result = service.review(
            user_message="Give me full IC analysis.",
            attachments=[deal_fixture],
            deal_id="PE-REAL-ATLAS-01",
            deal_name="Project Atlas",
            sector="Healthcare Services",
        )

    assert result["status"] == "ok"
    assert result["data"]["all_sections_present"] is True
    assert "pe-due-diligence-lifecycle" in result["data"]["selected_skills"]
    assert "deterministic_score" in result["data"]
