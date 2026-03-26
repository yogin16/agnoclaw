"""Tests for runtime rubric tuning helpers."""

from __future__ import annotations

from examples.pe_risk_platform.rubric_tools import set_weight_renormalized


def test_set_weight_renormalized_keeps_total_one():
    rubric = {
        "weights": {
            "leverage_debt_service": 0.24,
            "revenue_quality": 0.16,
            "earnings_quality": 0.15,
            "market_operational": 0.13,
            "legal_regulatory": 0.12,
            "management_governance": 0.10,
            "value_creation_execution": 0.10,
        }
    }

    updated = set_weight_renormalized(rubric, "revenue_quality", 0.30)
    weights = updated["weights"]

    assert abs(sum(weights.values()) - 1.0) < 1e-9
    assert abs(weights["revenue_quality"] - 0.30) < 1e-9
    assert weights["leverage_debt_service"] < 0.24


def test_set_weight_unknown_dimension_raises():
    rubric = {"weights": {"a": 0.5, "b": 0.5}}
    try:
        set_weight_renormalized(rubric, "missing", 0.2)
    except KeyError:
        pass
    else:
        raise AssertionError("Expected KeyError for unknown dimension")
