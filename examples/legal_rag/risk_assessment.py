"""
Example: Structured Risk Assessment — Pydantic models for contract risk scoring

Demonstrates using Agno's structured output with Pydantic models to produce
machine-readable risk assessments from contract analysis.

Run: uv run python examples/legal_rag/risk_assessment.py
"""

from __future__ import annotations

import sys
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _utils import detect_model

from agno.agent import Agent

MODEL = detect_model()


# ── Pydantic models for structured output ───────────────────────────────


class RiskLevel(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class RiskFactor(BaseModel):
    """A single identified risk in a contract."""

    clause_reference: str = Field(description="Section number and clause name")
    risk_level: RiskLevel
    description: str = Field(description="What the risk is")
    impact: str = Field(description="Potential consequences if risk materializes")
    recommendation: str = Field(description="Suggested mitigation or negotiation point")
    quote: str = Field(default="", description="Brief quote from the clause")


class ContractOverview(BaseModel):
    """High-level contract identification."""

    contract_type: str = Field(description="NDA, MSA, SaaS Agreement, etc.")
    parties: list[str] = Field(description="Names of all parties")
    effective_date: str
    expiration_date: str
    governing_law: str
    total_value: Optional[str] = None


class RiskAssessment(BaseModel):
    """Complete structured risk assessment of a contract."""

    overview: ContractOverview
    overall_risk_score: RiskLevel = Field(
        description="Overall risk level based on the worst individual risk"
    )
    risk_factors: list[RiskFactor] = Field(
        description="All identified risks, ordered by severity"
    )
    missing_clauses: list[str] = Field(
        default_factory=list,
        description="Standard clauses that are missing from the contract",
    )
    recommendations: list[str] = Field(
        description="Prioritized action items for legal review"
    )
    summary: str = Field(description="Executive summary of the assessment")


# ── Agent with structured output ────────────────────────────────────────

def assess_contract(contract_text: str) -> RiskAssessment:
    """
    Run structured risk assessment on a contract.

    Uses Agno's structured output to get a Pydantic model back from the LLM.
    """
    agent = Agent(
        model=MODEL,
        output_schema=RiskAssessment,
        instructions=(
            "You are a legal risk assessment specialist. Analyze the contract "
            "and produce a structured risk assessment. Be thorough but concise. "
            "Rate each risk factor independently. The overall risk score should "
            "reflect the worst individual risk found."
        ),
    )

    response = agent.run(
        f"Analyze this contract for risks:\n\n{contract_text}"
    )

    # Agno returns the structured output in response.content
    return response.content


# ── Demo ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    contracts_dir = Path(__file__).parent / "sample_contracts"

    for contract_file in sorted(contracts_dir.glob("*.txt")):
        print(f"\n{'=' * 60}")
        print(f"Risk Assessment: {contract_file.name}")
        print(f"{'=' * 60}")

        text = contract_file.read_text(encoding="utf-8")
        assessment = assess_contract(text)

        print(f"\nOverall Risk: {assessment.overall_risk_score.value.upper()}")
        print(f"Contract Type: {assessment.overview.contract_type}")
        print(f"Parties: {', '.join(assessment.overview.parties)}")
        print(f"Term: {assessment.overview.effective_date} → {assessment.overview.expiration_date}")
        print(f"Governing Law: {assessment.overview.governing_law}")

        print(f"\nRisk Factors ({len(assessment.risk_factors)}):")
        for rf in assessment.risk_factors:
            print(f"  [{rf.risk_level.value.upper():8s}] {rf.clause_reference}")
            print(f"            {rf.description}")
            print(f"            Impact: {rf.impact}")
            print(f"            Recommendation: {rf.recommendation}")

        if assessment.missing_clauses:
            print(f"\nMissing Clauses:")
            for mc in assessment.missing_clauses:
                print(f"  - {mc}")

        print(f"\nRecommendations:")
        for i, rec in enumerate(assessment.recommendations, 1):
            print(f"  {i}. {rec}")

        print(f"\nSummary: {assessment.summary}")
