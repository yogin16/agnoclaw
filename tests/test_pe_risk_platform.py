"""Contract checks for PE risk simulation assets."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from examples.pe_risk_platform.deal_pack_builder import (
    _extract_pptx_lines,
    _parse_metrics_from_text,
    build_pack_from_attachments,
)
from examples.pe_risk_platform.quality_gates import quality_gate
from examples.pe_risk_platform.scoring_engine import (
    DEFAULT_RUBRIC_PATH,
    score_deal,
    validate_against_expected,
)
from examples.pe_risk_platform.simulate_embedding import _write_skills_for_deal


def test_fixture_deals_match_expected_outcomes():
    rubric = json.loads(Path(DEFAULT_RUBRIC_PATH).read_text(encoding="utf-8"))
    deals_dir = Path("examples/pe_risk_platform/fixtures/deals")

    for deal_file in sorted(deals_dir.glob("*.json")):
        deal = json.loads(deal_file.read_text(encoding="utf-8"))
        result = score_deal(deal, rubric)
        validation = validate_against_expected(deal, result)
        assert validation["all_passed"], (
            f"Expected outcome mismatch for {deal_file.name}: {validation}"
        )


def test_pptx_metric_extraction_smoke(tmp_path):
    ppt_dir = tmp_path / "ppt" / "slides"
    ppt_dir.mkdir(parents=True)
    (ppt_dir / "slide1.xml").write_text(
        """
<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld><p:spTree><p:sp><p:txBody>
    <a:p><a:r><a:t>Total Leverage 5.0x</a:t></a:r></a:p>
    <a:p><a:r><a:t>Interest Coverage 2.1x</a:t></a:r></a:p>
    <a:p><a:r><a:t>Recurring Revenue 61%</a:t></a:r></a:p>
    <a:p><a:r><a:t>Top Customer 20%</a:t></a:r></a:p>
  </p:txBody></p:sp></p:spTree></p:cSld>
</p:sld>
""",
        encoding="utf-8",
    )

    pptx_path = tmp_path / "sample.pptx"
    with zipfile.ZipFile(pptx_path, "w") as archive:
        archive.write(ppt_dir / "slide1.xml", arcname="ppt/slides/slide1.xml")

    lines = _extract_pptx_lines(pptx_path)
    metrics, extracted = _parse_metrics_from_text(lines)

    assert len(lines) >= 4
    assert extracted["total_leverage_x"] == 5.0
    assert extracted["interest_coverage_x"] == 2.1
    assert extracted["recurring_revenue_pct"] == 61.0
    assert extracted["top_customer_pct"] == 20.0
    assert metrics["interest_coverage_x"] == 2.1


def test_quality_gate_fails_sparse_attachment_pack(tmp_path):
    note = tmp_path / "note.txt"
    note.write_text("This deck has no numeric underwriting metrics.", encoding="utf-8")

    pack, _report = build_pack_from_attachments(
        attachments=[note],
        deal_id="PE-REAL-FAIL-01",
        deal_name="Sparse Deal",
        sector="Unknown",
        sponsor="Test Sponsor",
        strategy="Control buyout",
    )

    gate = quality_gate(pack, min_overall_coverage=0.85, require_critical_complete=True)
    assert gate["passed"] is False
    assert gate["completeness"]["overall_coverage"] < 0.85
    assert len(gate["completeness"]["missing_critical_fields"]) > 0


def test_attachment_ingestion_with_deal_pack_json_passes_quality_gate():
    deal_path = Path(
        "examples/pe_risk_platform/fixtures/deals/PE-2026-001-northstar-vertical-software.json"
    )
    pack, _report = build_pack_from_attachments(
        attachments=[deal_path],
        deal_id="PE-OVERRIDE",
        deal_name="Override Name",
        sector="Unknown",
        sponsor="Test Sponsor",
        strategy="Control buyout",
    )
    gate = quality_gate(pack, min_overall_coverage=0.85, require_critical_complete=True)
    assert gate["passed"] is True
    assert pack["deal_id"] == "PE-2026-001"


def test_skill_template_includes_selected_rubric_path(tmp_path):
    skills_dir = tmp_path / "skills"
    deal_file = tmp_path / "deal.json"
    rubric_file = tmp_path / "rubric.json"

    deal_file.write_text(
        json.dumps(
            {
                "deal_id": "PE-TEST-1",
                "deal_name": "Deal",
                "sponsor": "Sponsor",
                "strategy": "Control buyout",
                "sector": "SaaS",
                "transaction": {
                    "enterprise_value_usd_mn": 1,
                    "entry_ebitda_multiple": 1,
                    "equity_check_usd_mn": 1,
                    "debt_package": {
                        "total_leverage_x": 4.0,
                        "cash_interest_rate_pct": 8.0,
                        "minimum_interest_coverage_x": 2.0,
                        "covenant_headroom_x": 1.0,
                    },
                },
                "metrics": {
                    "interest_coverage_x": 2.5,
                    "recurring_revenue_pct": 70,
                    "net_revenue_retention_pct": 102,
                    "top_customer_pct": 15,
                    "customer_churn_pct": 7,
                    "qoe_adjustments_pct_ebitda": 10,
                    "free_cash_flow_conversion_pct": 80,
                    "working_capital_volatility_pct_sales": 6,
                    "capex_pct_revenue": 6,
                    "end_market_cyclicality_1_to_5": 3,
                    "supplier_concentration_pct": 20,
                    "pricing_power_1_to_5": 3,
                    "regulatory_complexity_1_to_5": 2,
                    "open_litigation_count": 0,
                    "management_turnover_3y": 1,
                    "finance_team_depth_1_to_5": 4,
                    "board_reporting_maturity_1_to_5": 4,
                    "value_creation_dependency_1_to_5": 3,
                    "execution_complexity_1_to_5": 3,
                    "integration_risk_1_to_5": 2,
                },
                "diligence_flags": [],
                "open_diligence_questions": [],
            }
        ),
        encoding="utf-8",
    )
    rubric_file.write_text(
        json.dumps(
            {
                "rubric_id": "custom",
                "weights": {
                    "leverage_debt_service": 0.24,
                    "revenue_quality": 0.16,
                    "earnings_quality": 0.15,
                    "market_operational": 0.13,
                    "legal_regulatory": 0.12,
                    "management_governance": 0.10,
                    "value_creation_execution": 0.10,
                },
                "risk_bands": [{"name": "low", "max_score": 100, "default_action": "Proceed"}],
                "severity_penalties": {"low": 1.0, "medium": 3.0, "high": 7.0, "critical": 12.0},
            }
        ),
        encoding="utf-8",
    )

    _write_skills_for_deal(skills_dir, deal_file=deal_file, rubric_file=rubric_file)

    scoring_skill = (skills_dir / "pe-deal-scoring" / "SKILL.md").read_text(encoding="utf-8")
    assert str(rubric_file) in scoring_skill
