"""Backend-friendly PE risk review service (chat-style inputs).

This wraps agnoclaw AgentHarness for workflows where callers provide:
- a user message
- attached files (pptx/docx/xlsx/pdf/json/etc.)

The service normalizes attachments into deal_pack_v1, applies quality gates,
selects relevant PE skills, and returns structured outputs.
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path
from typing import Any

from agnoclaw import AgentHarness
from agnoclaw.runtime import ExecutionContext, InMemoryEventSink

try:  # package import
    from .deal_pack_builder import build_pack_from_attachments
    from .quality_gates import quality_gate
    from .scoring_engine import DEFAULT_RUBRIC_PATH, score_deal
except Exception:  # pragma: no cover - script execution fallback
    from deal_pack_builder import build_pack_from_attachments
    from quality_gates import quality_gate
    from scoring_engine import DEFAULT_RUBRIC_PATH, score_deal

ROOT = Path(__file__).resolve().parent
DUE_TEMPLATE = ROOT / "skills_templates" / "pe-due-diligence-lifecycle.SKILL.template.md"
SCORING_TEMPLATE = ROOT / "skills_templates" / "pe-deal-scoring.SKILL.template.md"


def _contains_sections(text: str, required: list[str]) -> dict[str, bool]:
    lowered = text.lower()
    return {section: section.lower() in lowered for section in required}


def _route_skills(user_message: str, enable_scoring: bool) -> list[str]:
    q = user_message.lower()
    wants_score = any(token in q for token in ["score", "risk band", "ic action", "reprice"])
    wants_due = any(
        token in q for token in ["diligence", "risk register", "go / no-go", "go-no-go"]
    )

    if wants_score and not wants_due:
        return ["pe-deal-scoring"] if enable_scoring else ["pe-due-diligence-lifecycle"]
    if wants_due and not wants_score:
        return ["pe-due-diligence-lifecycle"]
    if enable_scoring:
        return ["pe-due-diligence-lifecycle", "pe-deal-scoring"]
    return ["pe-due-diligence-lifecycle"]


class PERiskReviewService:
    def __init__(
        self,
        *,
        model: str,
        workspace_dir: str | Path,
        rubric_path: str | Path = DEFAULT_RUBRIC_PATH,
        min_completeness: float = 0.85,
    ) -> None:
        self.model = model
        self.workspace_dir = Path(workspace_dir).resolve()
        self.rubric_path = Path(rubric_path).resolve()
        self.min_completeness = min_completeness
        self.rubric = self._load_rubric()

        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.skills_dir = self.workspace_dir / "skills"
        self.skills_dir.mkdir(parents=True, exist_ok=True)

    def _load_rubric(self) -> dict[str, Any]:
        return json.loads(self.rubric_path.read_text(encoding="utf-8"))

    def _write_skills_for_deal(self, deal_file: Path) -> None:
        score_cmd = " ".join(
            [
                shlex.quote(sys.executable),
                shlex.quote(str(ROOT / "scoring_engine.py")),
                "score",
                shlex.quote(str(deal_file)),
                "--rubric",
                shlex.quote(str(self.rubric_path)),
                "--format",
                "text",
            ]
        )

        due_text = DUE_TEMPLATE.read_text(encoding="utf-8")
        score_text = SCORING_TEMPLATE.read_text(encoding="utf-8").replace(
            "__SCORING_COMMAND__", score_cmd
        )

        due_dir = self.skills_dir / "pe-due-diligence-lifecycle"
        scoring_dir = self.skills_dir / "pe-deal-scoring"
        due_dir.mkdir(parents=True, exist_ok=True)
        scoring_dir.mkdir(parents=True, exist_ok=True)
        (due_dir / "SKILL.md").write_text(due_text, encoding="utf-8")
        (scoring_dir / "SKILL.md").write_text(score_text, encoding="utf-8")

    def review(
        self,
        *,
        user_message: str,
        attachments: list[str | Path],
        deal_id: str,
        deal_name: str,
        sector: str,
        sponsor: str = "Agnoclaw Capital Partners",
        strategy: str = "Control buyout",
        allow_partial: bool = False,
        context: ExecutionContext | None = None,
    ) -> dict[str, Any]:
        # Reload rubric each review so runtime edits are applied immediately.
        self.rubric = self._load_rubric()

        attachment_paths = [Path(p).resolve() for p in attachments]
        deal_pack, ingestion_report = build_pack_from_attachments(
            attachments=attachment_paths,
            deal_id=deal_id,
            deal_name=deal_name,
            sector=sector,
            sponsor=sponsor,
            strategy=strategy,
        )

        gate = quality_gate(
            deal_pack,
            min_overall_coverage=self.min_completeness,
            require_critical_complete=not allow_partial,
        )
        if not gate["passed"] and not allow_partial:
            return {
                "status": "error",
                "error": {
                    "code": "QUALITY_GATE_FAILED",
                    "message": "Deal pack failed quality gate",
                    "details": gate,
                },
                "ingestion_report": ingestion_report,
            }

        deal_file = self.workspace_dir / f"{deal_id}.json"
        deal_file.write_text(json.dumps(deal_pack, indent=2), encoding="utf-8")

        self._write_skills_for_deal(deal_file)

        sink = InMemoryEventSink()
        harness = AgentHarness(
            model=self.model,
            workspace_dir=self.workspace_dir,
            event_sink=sink,
        )

        enable_scoring = gate["completeness"]["critical_coverage"] >= 1.0
        selected_skills = _route_skills(user_message, enable_scoring)

        outputs: dict[str, str] = {}
        checks: dict[str, dict[str, bool]] = {}

        deal_json = json.dumps(deal_pack, indent=2)
        for skill in selected_skills:
            if skill == "pe-due-diligence-lifecycle":
                required = self.rubric["output_contract"]["due_diligence_required_sections"]
            else:
                required = self.rubric["output_contract"]["deal_scoring_required_sections"]

            prompt = (
                f"User request: {user_message}\n\n"
                f"Use skill '{skill}'. Required sections: {', '.join(required)}.\n\n"
                f"Deal Context:\n```json\n{deal_json}\n```"
            )
            response = harness.run(prompt, skill=skill, context=context)
            text = str(getattr(response, "content", response))
            outputs[skill] = text
            checks[skill] = _contains_sections(text, required)

        deterministic_score = None
        if enable_scoring:
            deterministic_score = score_deal(deal_pack, self.rubric)

        events = [evt.event_type for evt in sink.events]

        return {
            "status": "ok",
            "data": {
                "deal_id": deal_id,
                "quality_gate": gate,
                "selected_skills": selected_skills,
                "section_checks": checks,
                "all_sections_present": all(all(v.values()) for v in checks.values()),
                "deterministic_score": deterministic_score,
                "outputs": outputs,
                "event_counts": {
                    "run.started": events.count("run.started"),
                    "skill.load.started": events.count("skill.load.started"),
                    "skill.load.completed": events.count("skill.load.completed"),
                    "run.completed": events.count("run.completed"),
                },
                "deal_pack_path": str(deal_file),
            },
            "ingestion_report": ingestion_report,
        }
