"""PE risk-management embedding tests for agnoclaw as a Python library."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agnoclaw.agent import AgentHarness
from agnoclaw.config import HarnessConfig
from agnoclaw.runtime import (
    ExecutionContext,
    HarnessError,
    InMemoryEventSink,
    PolicyDecision,
)
from agnoclaw.skills.registry import SkillRegistry


def _write_pe_skills(skills_root: Path) -> None:
    """Create two PE skills: reasoning-only and reasoning+computation."""
    due_skill_dir = skills_root / "pe-due-diligence-lifecycle"
    due_skill_dir.mkdir(parents=True, exist_ok=True)
    (due_skill_dir / "SKILL.md").write_text(
        """---
name: pe-due-diligence-lifecycle
description: Private equity due diligence lifecycle checklist and risk framing
allowed-tools: files
---

# PE Due Diligence Lifecycle Skill

Apply this lifecycle for private equity risk review:
1. Investment thesis validation
2. Commercial due diligence
3. Financial quality of earnings and cash conversion
4. Legal, compliance, and regulatory review
5. Value creation plan and 100-day priorities
6. IC memo risk register with mitigations

Output format:
- `Risk register` with severity (Low/Medium/High)
- `Open diligence questions`
- `Go / No-go recommendation` with rationale
""",
        encoding="utf-8",
    )

    scoring_skill_dir = skills_root / "pe-deal-scoring"
    scoring_skill_dir.mkdir(parents=True, exist_ok=True)
    scripts_dir = scoring_skill_dir / "scripts"
    data_dir = scoring_skill_dir / "data"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    deal_signal_path = data_dir / "deal_signal.json"
    deal_signal_path.write_text(
        json.dumps(
            {
                "leverage_ratio": 5.2,
                "interest_coverage": 1.8,
                "revenue_volatility": 0.25,
                "customer_concentration": 0.42,
                "pending_litigation": 1,
            }
        ),
        encoding="utf-8",
    )

    scorer_path = scripts_dir / "compute_deal_score.py"
    scorer_path.write_text(
        """import json
import sys
from pathlib import Path


def compute_score(payload: dict) -> int:
    score = 0.0
    score += payload.get("leverage_ratio", 0.0) * 8.0
    score += max(0.0, 3.0 - payload.get("interest_coverage", 0.0)) * 10.0
    score += payload.get("revenue_volatility", 0.0) * 40.0
    score += payload.get("customer_concentration", 0.0) * 30.0
    score += payload.get("pending_litigation", 0.0) * 12.0
    return max(0, min(100, round(score)))


def main() -> None:
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    score = compute_score(payload)
    band = "HIGH" if score >= 80 else "MEDIUM" if score >= 50 else "LOW"
    print(f"Computed Composite Risk Score: {score}")
    print(f"Risk Band: {band}")


if __name__ == "__main__":
    main()
""",
        encoding="utf-8",
    )

    score_cmd = " ".join(
        [
            shlex.quote(sys.executable),
            shlex.quote(str(scorer_path)),
            shlex.quote(str(deal_signal_path)),
        ]
    )

    (scoring_skill_dir / "SKILL.md").write_text(
        f"""---
name: pe-deal-scoring
description: PE deal scoring rubric with deterministic computation from signal pack
allowed-tools: bash, files
---

# PE Deal Scoring Skill

Use the scoring rubric below for private equity risk calibration.

## Quantitative Signal Snapshot
!`{score_cmd}`

## Interpretation Bands
- LOW: 0-49
- MEDIUM: 50-79
- HIGH: 80-100

## Output format
1. Explain top 3 score drivers
2. Recommend mitigations
3. State investment committee action (Proceed / Proceed with conditions / Pause)
""",
        encoding="utf-8",
    )


def _make_harness(tmp_path: Path, *, event_sink=None, policy_engine=None):
    mock_agent = MagicMock()

    def _agent_ctor(*args, **kwargs):
        del args
        mock_agent.system_message = kwargs.get("system_message")
        mock_agent.session_id = kwargs.get("session_id")
        return mock_agent

    with patch("agnoclaw.agent.Agent", side_effect=_agent_ctor):
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            harness = AgentHarness(
                workspace_dir=tmp_path,
                config=HarnessConfig(),
                event_sink=event_sink,
                policy_engine=policy_engine,
            )
    return harness, mock_agent


def test_pe_reasoning_skill_loads_without_inline_exec(tmp_path):
    skills_root = tmp_path / "skills"
    _write_pe_skills(skills_root)
    registry = SkillRegistry(workspace_skills_dir=skills_root, auto_approve_installs=True)

    content = registry.load_skill("pe-due-diligence-lifecycle", arguments="Northstar Logistics")

    assert content is not None
    assert "PE Due Diligence Lifecycle Skill" in content
    assert "Go / No-go recommendation" in content
    assert "!`" not in content


def test_pe_computation_skill_executes_inline_command_for_local_skills(tmp_path):
    skills_root = tmp_path / "skills"
    _write_pe_skills(skills_root)
    registry = SkillRegistry(workspace_skills_dir=skills_root, auto_approve_installs=True)

    content = registry.load_skill("pe-deal-scoring")

    assert content is not None
    assert "Computed Composite Risk Score: 88" in content
    assert "Risk Band: HIGH" in content
    assert "!`" not in content


def test_pe_computation_skill_blocks_inline_exec_for_community_skills(tmp_path):
    external_skills = tmp_path / "external-skills"
    _write_pe_skills(external_skills)

    workspace_skills = tmp_path / "workspace-skills"
    workspace_skills.mkdir(parents=True, exist_ok=True)
    registry = SkillRegistry(workspace_skills_dir=workspace_skills, auto_approve_installs=True)
    registry.add_directory(external_skills, trust="community")

    content = registry.load_skill("pe-deal-scoring")

    assert content is not None
    assert "Computed Composite Risk Score: 88" not in content
    assert "Risk Band: HIGH" not in content
    assert "!`" in content


def test_pe_library_embedding_e2e_with_events_and_execution_context(tmp_path):
    sink = InMemoryEventSink()
    harness, mock_agent = _make_harness(tmp_path, event_sink=sink)
    _write_pe_skills(harness.workspace.skills_dir())

    captured = {}

    def _run(*args, **kwargs):
        del args, kwargs
        captured["system_prompt"] = mock_agent.system_message
        return SimpleNamespace(content="IC recommendation prepared")

    mock_agent.run.side_effect = _run

    context = ExecutionContext.create(
        user_id="analyst-42",
        session_id="session-pe-001",
        workspace_id=str(harness.workspace.path),
        tenant_id="tenant-pe",
        org_id="org-investments",
        roles=["risk_analyst"],
        scopes=["pe.risk.assess"],
        request_id="req-pe-001",
        trace_id="trace-pe-001",
        metadata={"deal_id": "DL-0091"},
    )

    result = harness.run(
        "Assess downside risk for DL-0091 and provide IC action.",
        skill="pe-deal-scoring",
        context=context,
    )

    assert result.content == "IC recommendation prepared"
    assert "PE Deal Scoring Skill" in captured["system_prompt"]
    assert "Computed Composite Risk Score: 88" in captured["system_prompt"]

    event_types = [event.event_type for event in sink.events]
    assert "skill.load.started" in event_types
    assert "skill.load.completed" in event_types
    assert "run.completed" in event_types


def test_pe_skill_load_can_be_policy_denied_by_role(tmp_path):
    class RoleRestrictedSkillPolicy:
        def before_run(self, run_input, context):
            del run_input, context
            return PolicyDecision.allow()

        def before_prompt_send(self, prompt, context):
            del prompt, context
            return PolicyDecision.allow()

        def before_skill_load(self, request, context):
            if request.name == "pe-deal-scoring" and "risk_analyst" not in context.roles:
                return PolicyDecision.deny(
                    reason_code="ROLE_REQUIRED",
                    message="pe-deal-scoring requires risk_analyst role",
                )
            return PolicyDecision.allow()

    harness, mock_agent = _make_harness(tmp_path, policy_engine=RoleRestrictedSkillPolicy())
    _write_pe_skills(harness.workspace.skills_dir())
    mock_agent.run.return_value = SimpleNamespace(content="should not run")

    context = ExecutionContext.create(
        user_id="viewer-10",
        session_id="session-pe-002",
        workspace_id=str(harness.workspace.path),
        roles=["observer"],
    )

    with pytest.raises(HarnessError, match="requires risk_analyst role") as exc:
        harness.run(
            "Score this deal and summarize risk.",
            skill="pe-deal-scoring",
            context=context,
        )

    assert exc.value.code == "POLICY_DENIED"
    mock_agent.run.assert_not_called()
