# deal_pack_v1 Schema

`deal_pack_v1` is the normalized contract between ingestion, deterministic scoring, skill prompting, and IC output validation.

## Top-Level Fields

- `deal_id` (string, required)
- `deal_name` (string, required)
- `sponsor` (string, required)
- `strategy` (string, required)
- `sector` (string, required)
- `investment_thesis` (string, required)
- `transaction` (object, required)
- `metrics` (object, required)
- `diligence_flags` (array, required)
- `open_diligence_questions` (array, required)
- `source_material` (object, optional)
- `expected_outcome` (object, optional; simulation-only)

## transaction.debt_package

Required numeric fields:
- `total_leverage_x`
- `cash_interest_rate_pct`
- `minimum_interest_coverage_x`
- `covenant_headroom_x`

## metrics Fields

Required numeric fields:
- `interest_coverage_x`
- `recurring_revenue_pct`
- `net_revenue_retention_pct`
- `top_customer_pct`
- `customer_churn_pct`
- `qoe_adjustments_pct_ebitda`
- `free_cash_flow_conversion_pct`
- `working_capital_volatility_pct_sales`
- `capex_pct_revenue`
- `end_market_cyclicality_1_to_5`
- `supplier_concentration_pct`
- `pricing_power_1_to_5`
- `regulatory_complexity_1_to_5`
- `open_litigation_count`
- `management_turnover_3y`
- `finance_team_depth_1_to_5`
- `board_reporting_maturity_1_to_5`
- `value_creation_dependency_1_to_5`
- `execution_complexity_1_to_5`
- `integration_risk_1_to_5`

In ingestion stage, fields may be `null` before enrichment.  
For scoring and IC-grade review, quality gates require these fields to be populated numerically.

## diligence_flags item

Required fields:
- `id` (string)
- `severity` (enum: `low`, `medium`, `high`, `critical`)
- `dimension` (enum: `leverage_debt_service`, `revenue_quality`, `earnings_quality`, `market_operational`, `legal_regulatory`, `management_governance`, `value_creation_execution`)
- `description` (string)

## expected_outcome (simulation-only)

- `expected_risk_band` (enum: `low`, `medium`, `high`, `severe`)
- `expected_action` (string)
- `score_range` ([min, max])

## source_material (real-deal onboarding)

Suggested fields:
- `type` (`json`, `pptx`, `mixed`)
- `path`
- `line_count`
- `extracted_fields`
- `sample_lines`

## Quality Gates

Use `quality_gates.py` to enforce:
- schema validity
- completeness threshold (`overall_coverage`)
- critical field completeness for firm-grade scoring workflows
