"""
Example: Multi-Agent Contract Analysis Team

Demonstrates a team of specialized agents working together:
  1. Extractor — reads the contract and extracts structured data
  2. Risk Analyst — identifies risks and rates severity
  3. Compliance Checker — verifies regulatory compliance
  4. Summary Writer — produces the final report

Uses Agno's Agent team pattern with AgentHarness integration.

Run: uv run python examples/legal_rag/contract_analysis_team.py
Requires: ANTHROPIC_API_KEY
"""

from pathlib import Path

from agno.agent import Agent
from agno.team.team import Team

from agnoclaw import AgentHarness


# ── Specialized agents ──────────────────────────────────────────────────

extractor = Agent(
    name="Contract Extractor",
    model="anthropic:claude-sonnet-4-6",
    role="Extract key terms",
    instructions=(
        "You are a contract data extraction specialist. When given a contract, "
        "extract and organize:\n"
        "- Parties (full names, roles)\n"
        "- Key dates (effective, expiration, renewal)\n"
        "- Financial terms (fees, caps, penalties)\n"
        "- IP and confidentiality provisions\n"
        "- Termination conditions\n"
        "Format as structured bullet points."
    ),
)

risk_analyst = Agent(
    name="Risk Analyst",
    model="anthropic:claude-sonnet-4-6",
    role="Assess contract risks",
    instructions=(
        "You are a contract risk analyst. Review the extracted terms and:\n"
        "- Identify each risk factor with severity (critical/high/medium/low)\n"
        "- Flag one-sided clauses\n"
        "- Note missing standard protections\n"
        "- Assess liability exposure\n"
        "- Rate overall risk on a 1-10 scale"
    ),
)

compliance_checker = Agent(
    name="Compliance Checker",
    model="anthropic:claude-sonnet-4-6",
    role="Check regulatory compliance",
    instructions=(
        "You are a regulatory compliance specialist. Review the contract for:\n"
        "- Data protection compliance (GDPR, CCPA if applicable)\n"
        "- Industry-specific requirements\n"
        "- Jurisdiction-specific legal requirements\n"
        "- Standard contract law compliance\n"
        "Flag any compliance gaps."
    ),
)

summary_writer = Agent(
    name="Summary Writer",
    model="anthropic:claude-sonnet-4-6",
    role="Write final analysis report",
    instructions=(
        "You are a legal report writer. Synthesize the findings from the "
        "Extractor, Risk Analyst, and Compliance Checker into a clear, "
        "actionable report with:\n"
        "1. Executive Summary (2-3 sentences)\n"
        "2. Key Terms Overview\n"
        "3. Risk Assessment Table\n"
        "4. Compliance Status\n"
        "5. Prioritized Recommendations\n"
        "Write for a business audience, not lawyers."
    ),
)


# ── Team assembly ───────────────────────────────────────────────────────

analysis_team = Team(
    name="Contract Analysis Team",
    mode="coordinate",
    model="anthropic:claude-sonnet-4-6",
    members=[extractor, risk_analyst, compliance_checker, summary_writer],
    instructions=(
        "You coordinate a contract analysis team. For each contract:\n"
        "1. First, have the Extractor pull out key terms\n"
        "2. Then, Risk Analyst and Compliance Checker work in parallel\n"
        "3. Finally, Summary Writer produces the final report\n"
        "Ensure all team members get the full contract text."
    ),
    markdown=True,
)


# ── Demo ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    contracts_dir = Path(__file__).parent / "sample_contracts"

    # Analyze each contract with the full team
    for contract_file in sorted(contracts_dir.glob("*.txt")):
        print(f"\n{'=' * 70}")
        print(f"Team Analysis: {contract_file.name}")
        print(f"{'=' * 70}")

        text = contract_file.read_text(encoding="utf-8")

        # Run the team analysis
        analysis_team.print_response(
            f"Analyze this contract:\n\n{text}",
            stream=True,
        )
        print()

    # ── Also demonstrate with AgentHarness wrapping ─────────────────────
    print("\n" + "=" * 70)
    print("AgentHarness Integration (embedded library pattern)")
    print("=" * 70)

    # Wrap the team leader as the AgentHarness's underlying agent
    harness = AgentHarness(
        name="contract-team-harness",
        instructions=(
            "You are a contract analysis coordinator. Use your team of "
            "specialists to analyze contracts thoroughly."
        ),
    )

    # For embedded SaaS: analyze a specific contract
    nda_text = (contracts_dir / "nda.txt").read_text(encoding="utf-8")
    print("\n[AgentHarness + Team] Analyzing NDA...")
    harness.print_response(
        f"Analyze this NDA and highlight the top 3 risks:\n\n{nda_text[:3000]}",
        stream=True,
    )
