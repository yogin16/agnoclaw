"""Deterministic PE deal risk scoring engine for simulation fixtures.

Run examples:
  uv run python examples/pe_risk_platform/scoring_engine.py score \
    examples/pe_risk_platform/fixtures/deals/PE-2026-001-northstar-vertical-software.json

  uv run python examples/pe_risk_platform/scoring_engine.py batch \
    examples/pe_risk_platform/fixtures/deals --format json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:  # package import
    from .quality_gates import quality_gate
except Exception:  # pragma: no cover - script execution fallback
    from quality_gates import quality_gate

ROOT = Path(__file__).resolve().parent
DEFAULT_RUBRIC_PATH = ROOT / "rubrics" / "pe_deal_risk_rubric_v1.json"


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _score_from_upper_bounds(value: float, buckets: list[tuple[float, float]]) -> float:
    """Buckets are sorted ascending by threshold: (upper_bound, risk_score)."""
    for upper, score in buckets:
        if value <= upper:
            return score
    return buckets[-1][1]


def _score_from_lower_bounds(value: float, buckets: list[tuple[float, float]]) -> float:
    """Buckets are sorted descending by threshold: (lower_bound, risk_score)."""
    for lower, score in buckets:
        if value >= lower:
            return score
    return buckets[-1][1]


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _dimension_scores(metrics: dict[str, float], flags: list[dict[str, Any]]) -> dict[str, float]:
    leverage = _score_from_upper_bounds(
        metrics["total_leverage_x"],
        [(3.0, 10), (3.5, 25), (4.0, 40), (4.5, 55), (5.0, 70), (5.5, 82), (99.0, 95)],
    )
    interest = _score_from_upper_bounds(
        metrics["interest_coverage_x"],
        [(1.5, 95), (2.0, 80), (2.5, 65), (3.0, 50), (3.5, 35), (4.0, 20), (99.0, 10)],
    )
    covenant_headroom = _score_from_upper_bounds(
        metrics["covenant_headroom_x"],
        [(0.2, 95), (0.4, 80), (0.7, 65), (1.0, 45), (1.5, 25), (99.0, 10)],
    )
    leverage_debt_service = round(0.45 * leverage + 0.45 * interest + 0.10 * covenant_headroom, 1)

    recurring = _score_from_lower_bounds(
        metrics["recurring_revenue_pct"],
        [(85, 10), (70, 25), (55, 45), (40, 65), (0, 85)],
    )
    nrr = _score_from_lower_bounds(
        metrics["net_revenue_retention_pct"],
        [(110, 10), (105, 20), (100, 35), (95, 55), (90, 75), (0, 90)],
    )
    top_customer = _score_from_upper_bounds(
        metrics["top_customer_pct"],
        [(8, 10), (15, 30), (22, 50), (30, 70), (100, 88)],
    )
    churn = _score_from_upper_bounds(
        metrics["customer_churn_pct"],
        [(4, 10), (7, 25), (10, 45), (14, 65), (100, 85)],
    )
    revenue_quality = round(0.35 * recurring + 0.25 * nrr + 0.25 * top_customer + 0.15 * churn, 1)

    qoe = _score_from_upper_bounds(
        metrics["qoe_adjustments_pct_ebitda"],
        [(5, 12), (10, 25), (15, 45), (20, 65), (30, 82), (100, 95)],
    )
    fcf = _score_from_lower_bounds(
        metrics["free_cash_flow_conversion_pct"],
        [(90, 10), (80, 20), (70, 35), (60, 50), (50, 70), (0, 88)],
    )
    working_cap = _score_from_upper_bounds(
        metrics["working_capital_volatility_pct_sales"],
        [(3, 10), (6, 25), (10, 45), (15, 65), (100, 85)],
    )
    capex = _score_from_upper_bounds(
        metrics["capex_pct_revenue"],
        [(4, 15), (7, 30), (10, 50), (14, 70), (100, 88)],
    )
    earnings_quality = round(0.35 * qoe + 0.35 * fcf + 0.20 * working_cap + 0.10 * capex, 1)

    cyclicality = _score_from_upper_bounds(
        metrics["end_market_cyclicality_1_to_5"],
        [(1, 10), (2, 25), (3, 45), (4, 70), (5, 88)],
    )
    supplier = _score_from_upper_bounds(
        metrics["supplier_concentration_pct"],
        [(15, 10), (25, 25), (35, 45), (45, 65), (100, 85)],
    )
    pricing = _score_from_lower_bounds(
        metrics["pricing_power_1_to_5"],
        [(5, 10), (4, 25), (3, 45), (2, 65), (1, 85)],
    )
    market_operational = round(0.40 * cyclicality + 0.35 * supplier + 0.25 * pricing, 1)

    legal_complexity = _score_from_upper_bounds(
        metrics["regulatory_complexity_1_to_5"],
        [(1, 10), (2, 25), (3, 45), (4, 65), (5, 82)],
    )
    litigation = _score_from_upper_bounds(
        metrics["open_litigation_count"],
        [(0, 10), (1, 35), (2, 55), (3, 72), (100, 88)],
    )
    legal_flags = [f for f in flags if f.get("dimension") == "legal_regulatory"]
    legal_flag_penalty = 0.0
    for flag in legal_flags:
        sev = str(flag.get("severity", "")).lower()
        if sev == "critical":
            legal_flag_penalty += 18
        elif sev == "high":
            legal_flag_penalty += 10
        elif sev == "medium":
            legal_flag_penalty += 5
        elif sev == "low":
            legal_flag_penalty += 2
    legal_regulatory = round(
        _clamp((0.55 * legal_complexity + 0.45 * litigation) + min(25, legal_flag_penalty) * 0.7), 1
    )

    turnover = _score_from_upper_bounds(
        metrics["management_turnover_3y"],
        [(1, 12), (2, 28), (3, 45), (4, 65), (100, 85)],
    )
    finance_depth = _score_from_lower_bounds(
        metrics["finance_team_depth_1_to_5"],
        [(5, 10), (4, 25), (3, 45), (2, 65), (1, 85)],
    )
    board_reporting = _score_from_lower_bounds(
        metrics["board_reporting_maturity_1_to_5"],
        [(5, 10), (4, 25), (3, 45), (2, 65), (1, 85)],
    )
    management_governance = round(
        0.45 * turnover + 0.35 * finance_depth + 0.20 * board_reporting, 1
    )

    vc_dependency = _score_from_upper_bounds(
        metrics["value_creation_dependency_1_to_5"],
        [(1, 12), (2, 28), (3, 45), (4, 68), (5, 88)],
    )
    execution_complexity = _score_from_upper_bounds(
        metrics["execution_complexity_1_to_5"],
        [(1, 12), (2, 28), (3, 45), (4, 68), (5, 88)],
    )
    integration_risk = _score_from_upper_bounds(
        metrics["integration_risk_1_to_5"],
        [(1, 12), (2, 28), (3, 45), (4, 68), (5, 88)],
    )
    value_creation_execution = round(
        0.40 * vc_dependency + 0.35 * execution_complexity + 0.25 * integration_risk, 1
    )

    return {
        "leverage_debt_service": leverage_debt_service,
        "revenue_quality": revenue_quality,
        "earnings_quality": earnings_quality,
        "market_operational": market_operational,
        "legal_regulatory": legal_regulatory,
        "management_governance": management_governance,
        "value_creation_execution": value_creation_execution,
    }


def score_deal(
    deal: dict[str, Any],
    rubric: dict[str, Any],
) -> dict[str, Any]:
    gate = quality_gate(deal, min_overall_coverage=1.0, require_critical_complete=True)
    if not gate["passed"]:
        raise ValueError("Deal pack failed scoring gate: " + "; ".join(gate["errors"]))

    metrics = dict(deal.get("metrics", {}))
    debt = deal.get("transaction", {}).get("debt_package", {})
    metrics["total_leverage_x"] = float(debt.get("total_leverage_x", 0.0))
    metrics["covenant_headroom_x"] = float(debt.get("covenant_headroom_x", 0.0))

    flags: list[dict[str, Any]] = list(deal.get("diligence_flags", []))
    dimension_scores = _dimension_scores(metrics, flags)

    weights = rubric["weights"]
    weighted_score = sum(dimension_scores[k] * float(weights[k]) for k in dimension_scores)

    severity_penalties = rubric["severity_penalties"]
    flag_penalty_raw = 0.0
    severity_counts = {"low": 0, "medium": 0, "high": 0, "critical": 0}
    for flag in flags:
        sev = str(flag.get("severity", "")).lower()
        if sev in severity_counts:
            severity_counts[sev] += 1
            flag_penalty_raw += float(severity_penalties[sev])

    flag_penalty = min(15.0, flag_penalty_raw * 0.6)
    composite = round(_clamp(weighted_score + flag_penalty), 1)

    band = "severe"
    recommended_action = "No-go until mitigated"
    for entry in rubric["risk_bands"]:
        if composite <= float(entry["max_score"]):
            band = str(entry["name"])
            recommended_action = str(entry["default_action"])
            break

    top_drivers = sorted(dimension_scores.items(), key=lambda item: item[1], reverse=True)[:3]

    return {
        "deal_id": deal["deal_id"],
        "deal_name": deal["deal_name"],
        "rubric_id": rubric["rubric_id"],
        "composite_risk_score": composite,
        "risk_band": band,
        "recommended_action": recommended_action,
        "weighted_score_before_penalty": round(weighted_score, 1),
        "flag_penalty": round(flag_penalty, 1),
        "severity_counts": severity_counts,
        "dimension_scores": dimension_scores,
        "top_score_drivers": [{"dimension": name, "score": score} for name, score in top_drivers],
    }


def validate_against_expected(
    deal: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    expected = deal.get("expected_outcome") or {}
    low, high = expected.get("score_range", [0, 100])
    expected_band = expected.get("expected_risk_band")
    expected_action = expected.get("expected_action")

    actual_score = float(result["composite_risk_score"])
    score_in_range = float(low) <= actual_score <= float(high)
    band_match = (expected_band is None) or (str(expected_band) == str(result["risk_band"]))
    action_match = (expected_action is None) or (
        str(expected_action) == str(result["recommended_action"])
    )

    return {
        "score_in_range": score_in_range,
        "band_match": band_match,
        "action_match": action_match,
        "all_passed": score_in_range and band_match and action_match,
        "expected": expected,
        "actual": {
            "score": actual_score,
            "band": result["risk_band"],
            "action": result["recommended_action"],
        },
    }


def _format_text(result: dict[str, Any], validation: dict[str, Any] | None = None) -> str:
    lines = [
        f"Deal: {result['deal_id']} - {result['deal_name']}",
        f"Composite Risk Score: {result['composite_risk_score']}",
        f"Risk Band: {result['risk_band']}",
        f"Investment Committee Action: {result['recommended_action']}",
        f"Weighted Score (pre-penalty): {result['weighted_score_before_penalty']}",
        f"Diligence Flag Penalty: {result['flag_penalty']}",
        "Top Score Drivers:",
    ]
    for driver in result["top_score_drivers"]:
        lines.append(f"- {driver['dimension']}: {driver['score']}")
    if validation is not None:
        lines.append(
            "Expectation Check: "
            + ("PASS" if validation["all_passed"] else "FAIL")
            + f" (score_in_range={validation['score_in_range']}, "
            + f"band_match={validation['band_match']}, action_match={validation['action_match']})"
        )
    return "\n".join(lines)


def _cmd_score(args: argparse.Namespace) -> int:
    deal = _load_json(Path(args.deal_file))
    rubric = _load_json(Path(args.rubric))
    result = score_deal(deal, rubric)
    validation = validate_against_expected(deal, result)

    if args.format == "json":
        payload = {
            "status": "ok",
            "data": {"result": result, "validation": validation},
            "warnings": [],
        }
        print(json.dumps(payload, indent=2))
    else:
        print(_format_text(result, validation))

    if args.fail_on_mismatch and not validation["all_passed"]:
        return 1
    return 0


def _cmd_batch(args: argparse.Namespace) -> int:
    deals_dir = Path(args.deals_dir)
    rubric = _load_json(Path(args.rubric))

    results = []
    failed = []
    for deal_file in sorted(deals_dir.glob("*.json")):
        deal = _load_json(deal_file)
        result = score_deal(deal, rubric)
        validation = validate_against_expected(deal, result)
        record = {
            "deal_file": str(deal_file),
            "result": result,
            "validation": validation,
        }
        results.append(record)
        if not validation["all_passed"]:
            failed.append(record)

    if args.format == "json":
        payload = {
            "status": "ok" if not failed else "error",
            "data": {
                "total": len(results),
                "failed": len(failed),
                "results": results,
            },
            "warnings": [],
        }
        print(json.dumps(payload, indent=2))
    else:
        for record in results:
            print(_format_text(record["result"], record["validation"]))
            print("-" * 72)
        print(f"Batch Summary: total={len(results)} failed={len(failed)}")

    if args.fail_on_mismatch and failed:
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pe-risk-scoring",
        description="Deterministic PE downside-risk scoring for IC workflow simulation.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    score_cmd = sub.add_parser("score", help="Score one deal fixture JSON file")
    score_cmd.add_argument("deal_file", help="Path to deal fixture JSON")
    score_cmd.add_argument(
        "--rubric",
        default=str(DEFAULT_RUBRIC_PATH),
        help="Path to rubric JSON (default: pe_deal_risk_rubric_v1.json)",
    )
    score_cmd.add_argument("--format", choices=["text", "json"], default="text")
    score_cmd.add_argument(
        "--fail-on-mismatch",
        action="store_true",
        help="Return non-zero if expected fixture outcome checks fail.",
    )
    score_cmd.set_defaults(func=_cmd_score)

    batch_cmd = sub.add_parser("batch", help="Score all deal fixtures in a directory")
    batch_cmd.add_argument("deals_dir", help="Directory containing deal fixture JSON files")
    batch_cmd.add_argument(
        "--rubric",
        default=str(DEFAULT_RUBRIC_PATH),
        help="Path to rubric JSON (default: pe_deal_risk_rubric_v1.json)",
    )
    batch_cmd.add_argument("--format", choices=["text", "json"], default="text")
    batch_cmd.add_argument(
        "--fail-on-mismatch",
        action="store_true",
        help="Return non-zero if any expected fixture outcome check fails.",
    )
    batch_cmd.set_defaults(func=_cmd_batch)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        return int(args.func(args))
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except json.JSONDecodeError as exc:
        print(f"error: invalid JSON: {exc}", file=sys.stderr)
        return 2
    except KeyError as exc:
        print(f"error: missing required field: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
