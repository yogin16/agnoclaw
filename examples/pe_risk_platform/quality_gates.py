"""Schema and completeness gates for deal_pack_v1."""

from __future__ import annotations

from typing import Any

REQUIRED_TOP_LEVEL = [
    "deal_id",
    "deal_name",
    "sponsor",
    "strategy",
    "sector",
    "transaction",
    "metrics",
    "diligence_flags",
    "open_diligence_questions",
]

REQUIRED_DEBT_FIELDS = [
    "total_leverage_x",
    "cash_interest_rate_pct",
    "minimum_interest_coverage_x",
    "covenant_headroom_x",
]

REQUIRED_METRICS = [
    "interest_coverage_x",
    "recurring_revenue_pct",
    "net_revenue_retention_pct",
    "top_customer_pct",
    "customer_churn_pct",
    "qoe_adjustments_pct_ebitda",
    "free_cash_flow_conversion_pct",
    "working_capital_volatility_pct_sales",
    "capex_pct_revenue",
    "end_market_cyclicality_1_to_5",
    "supplier_concentration_pct",
    "pricing_power_1_to_5",
    "regulatory_complexity_1_to_5",
    "open_litigation_count",
    "management_turnover_3y",
    "finance_team_depth_1_to_5",
    "board_reporting_maturity_1_to_5",
    "value_creation_dependency_1_to_5",
    "execution_complexity_1_to_5",
    "integration_risk_1_to_5",
]

CRITICAL_FIELDS = [
    "transaction.debt_package.total_leverage_x",
    "metrics.interest_coverage_x",
    "metrics.recurring_revenue_pct",
    "metrics.net_revenue_retention_pct",
    "metrics.top_customer_pct",
    "metrics.qoe_adjustments_pct_ebitda",
    "metrics.free_cash_flow_conversion_pct",
    "metrics.regulatory_complexity_1_to_5",
]


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _get_path(pack: dict[str, Any], path: str) -> Any:
    current: Any = pack
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def validate_schema(pack: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []

    for key in REQUIRED_TOP_LEVEL:
        if key not in pack:
            errors.append(f"Missing top-level field: {key}")

    tx = pack.get("transaction")
    debt = tx.get("debt_package") if isinstance(tx, dict) else None
    if not isinstance(debt, dict):
        errors.append("Missing object: transaction.debt_package")
    else:
        for field in REQUIRED_DEBT_FIELDS:
            if field not in debt:
                errors.append(f"Missing debt field: transaction.debt_package.{field}")

    metrics = pack.get("metrics")
    if not isinstance(metrics, dict):
        errors.append("Missing object: metrics")
    else:
        for field in REQUIRED_METRICS:
            if field not in metrics:
                errors.append(f"Missing metric field: metrics.{field}")

    flags = pack.get("diligence_flags")
    if flags is not None and not isinstance(flags, list):
        errors.append("diligence_flags must be a list")

    questions = pack.get("open_diligence_questions")
    if questions is not None and not isinstance(questions, list):
        errors.append("open_diligence_questions must be a list")

    return {
        "is_valid": not errors,
        "errors": errors,
        "warnings": warnings,
    }


def compute_completeness(pack: dict[str, Any]) -> dict[str, Any]:
    debt = (pack.get("transaction") or {}).get("debt_package") or {}
    metrics = pack.get("metrics") or {}

    present_debt: list[str] = []
    missing_debt: list[str] = []
    for field in REQUIRED_DEBT_FIELDS:
        value = debt.get(field)
        if _is_number(value):
            present_debt.append(field)
        else:
            missing_debt.append(field)

    present_metrics: list[str] = []
    missing_metrics: list[str] = []
    for field in REQUIRED_METRICS:
        value = metrics.get(field)
        if _is_number(value):
            present_metrics.append(field)
        else:
            missing_metrics.append(field)

    present_critical: list[str] = []
    missing_critical: list[str] = []
    for field in CRITICAL_FIELDS:
        value = _get_path(pack, field)
        if _is_number(value):
            present_critical.append(field)
        else:
            missing_critical.append(field)

    debt_coverage = len(present_debt) / len(REQUIRED_DEBT_FIELDS)
    metrics_coverage = len(present_metrics) / len(REQUIRED_METRICS)
    critical_coverage = len(present_critical) / len(CRITICAL_FIELDS)
    overall_coverage = (
        (0.35 * debt_coverage) + (0.45 * metrics_coverage) + (0.20 * critical_coverage)
    )

    return {
        "debt_coverage": round(debt_coverage, 3),
        "metrics_coverage": round(metrics_coverage, 3),
        "critical_coverage": round(critical_coverage, 3),
        "overall_coverage": round(overall_coverage, 3),
        "missing_debt_fields": missing_debt,
        "missing_metric_fields": missing_metrics,
        "missing_critical_fields": missing_critical,
    }


def quality_gate(
    pack: dict[str, Any],
    *,
    min_overall_coverage: float = 0.85,
    require_critical_complete: bool = True,
) -> dict[str, Any]:
    schema = validate_schema(pack)
    completeness = compute_completeness(pack)

    gate_errors: list[str] = []
    if not schema["is_valid"]:
        gate_errors.extend(schema["errors"])

    if completeness["overall_coverage"] < min_overall_coverage:
        gate_errors.append(
            "overall_coverage below threshold: "
            f"{completeness['overall_coverage']} < {min_overall_coverage}"
        )

    if require_critical_complete and completeness["missing_critical_fields"]:
        gate_errors.append(
            "critical fields missing: " + ", ".join(completeness["missing_critical_fields"])
        )

    return {
        "passed": not gate_errors,
        "errors": gate_errors,
        "schema": schema,
        "completeness": completeness,
    }
