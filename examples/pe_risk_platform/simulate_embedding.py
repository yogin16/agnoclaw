"""Run PE risk workflow simulation with agnoclaw embedded as a library.

Modes:
- contract: deterministic scoring + skill-render checks (no model call)
- live: real model calls through AgentHarness with skill injection
- both: contract checks + live run

This runner supports two data-entry patterns:
- pre-normalized deal packs (`--deal-file`, `--deals-dir`)
- chat-like attachment ingestion (`--attachment ...`) that builds deal_pack_v1 first
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import tempfile
from pathlib import Path
from typing import Any

from agnoclaw import AgentHarness
from agnoclaw.runtime import InMemoryEventSink
from agnoclaw.skills.registry import SkillRegistry

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:  # package import
    from .deal_pack_builder import build_pack_from_attachments  # noqa: E402
    from .quality_gates import quality_gate  # noqa: E402
    from .scoring_engine import (  # noqa: E402
        DEFAULT_RUBRIC_PATH,
        score_deal,
        validate_against_expected,
    )
except Exception:  # pragma: no cover - script execution fallback
    from deal_pack_builder import build_pack_from_attachments  # noqa: E402
    from quality_gates import quality_gate  # noqa: E402
    from scoring_engine import (  # noqa: E402
        DEFAULT_RUBRIC_PATH,
        score_deal,
        validate_against_expected,
    )

try:
    from examples._utils import detect_model  # noqa: E402
except Exception:  # pragma: no cover
    detect_model = None


DUE_TEMPLATE = ROOT / "skills_templates" / "pe-due-diligence-lifecycle.SKILL.template.md"
SCORING_TEMPLATE = ROOT / "skills_templates" / "pe-deal-scoring.SKILL.template.md"


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _collect_deal_files(deals_dir: str | None, deal_files: list[str]) -> list[Path]:
    files = [Path(p).resolve() for p in deal_files]
    if deals_dir and deals_dir.strip():
        files.extend(sorted(Path(deals_dir).resolve().glob("*.json")))

    dedup: list[Path] = []
    seen: set[Path] = set()
    for deal_file in files:
        if deal_file not in seen:
            seen.add(deal_file)
            dedup.append(deal_file)
    return dedup


def _write_skills_for_deal(
    workspace_skills_dir: Path,
    *,
    deal_file: Path,
    rubric_file: Path,
) -> None:
    workspace_skills_dir.mkdir(parents=True, exist_ok=True)

    due_text = DUE_TEMPLATE.read_text(encoding="utf-8")

    score_cmd = " ".join(
        [
            shlex.quote(sys.executable),
            shlex.quote(str(ROOT / "scoring_engine.py")),
            "score",
            shlex.quote(str(deal_file)),
            "--rubric",
            shlex.quote(str(rubric_file)),
            "--format",
            "text",
        ]
    )
    scoring_text = SCORING_TEMPLATE.read_text(encoding="utf-8").replace(
        "__SCORING_COMMAND__", score_cmd
    )

    due_dir = workspace_skills_dir / "pe-due-diligence-lifecycle"
    score_dir = workspace_skills_dir / "pe-deal-scoring"
    due_dir.mkdir(parents=True, exist_ok=True)
    score_dir.mkdir(parents=True, exist_ok=True)

    (due_dir / "SKILL.md").write_text(due_text, encoding="utf-8")
    (score_dir / "SKILL.md").write_text(scoring_text, encoding="utf-8")


def _contains_sections(text: str, required_sections: list[str]) -> dict[str, bool]:
    normalized = text.lower()
    return {section: (section.lower() in normalized) for section in required_sections}


def _route_skills(user_query: str, *, enable_scoring: bool) -> list[str]:
    q = user_query.lower()

    score_terms = ["score", "risk band", "ic action", "investment committee action", "reprice"]
    due_terms = [
        "due diligence",
        "risk register",
        "go / no-go",
        "go-no-go",
        "diligence",
        "thesis",
    ]

    wants_score = any(term in q for term in score_terms)
    wants_due = any(term in q for term in due_terms)

    if wants_score and not wants_due:
        return ["pe-deal-scoring"] if enable_scoring else ["pe-due-diligence-lifecycle"]

    if wants_due and not wants_score:
        return ["pe-due-diligence-lifecycle"]

    # Default full workflow
    if enable_scoring:
        return ["pe-due-diligence-lifecycle", "pe-deal-scoring"]
    return ["pe-due-diligence-lifecycle"]


def _contract_check_for_deal(
    deal_file: Path,
    rubric: dict[str, Any],
    *,
    rubric_file: Path,
) -> dict[str, Any]:
    deal = _load_json(deal_file)

    with tempfile.TemporaryDirectory(prefix="pe-sim-workspace-") as tmp:
        workspace = Path(tmp)
        skills_dir = workspace / "skills"
        _write_skills_for_deal(skills_dir, deal_file=deal_file, rubric_file=rubric_file)

        registry = SkillRegistry(workspace_skills_dir=skills_dir, auto_approve_installs=True)
        due_content = registry.load_skill("pe-due-diligence-lifecycle") or ""
        score_content = registry.load_skill("pe-deal-scoring") or ""

        required_due_sections = rubric["output_contract"]["due_diligence_required_sections"]
        required_score_sections = rubric["output_contract"]["deal_scoring_required_sections"]
        due_sections_ok = _contains_sections(due_content, required_due_sections)
        score_sections_ok = _contains_sections(score_content, required_score_sections)

        try:
            scoring_result = score_deal(deal, rubric)
            validation = validate_against_expected(deal, scoring_result)
            score_string = f"Composite Risk Score: {scoring_result['composite_risk_score']}"
            score_snapshot_present = score_string in score_content
            scoring_error = None
        except Exception as exc:
            scoring_result = None
            validation = {
                "score_in_range": False,
                "band_match": False,
                "action_match": False,
                "all_passed": False,
                "expected": deal.get("expected_outcome", {}),
                "actual": {"error": str(exc)},
            }
            score_snapshot_present = False
            scoring_error = str(exc)

        return {
            "deal_file": str(deal_file),
            "deal_id": deal.get("deal_id"),
            "contract_mode": {
                "score_result": scoring_result,
                "fixture_validation": validation,
                "due_skill_sections_present": due_sections_ok,
                "score_skill_sections_present": score_sections_ok,
                "score_snapshot_in_skill_render": score_snapshot_present,
                "all_sections_present": all(due_sections_ok.values())
                and all(score_sections_ok.values()),
                "scoring_error": scoring_error,
            },
        }


def _run_skill_with_contract(
    harness: AgentHarness,
    *,
    skill: str,
    deal_json: str,
    required_sections: list[str],
    user_query: str,
) -> tuple[str, dict[str, bool]]:
    req = ", ".join(required_sections)
    prompt = (
        f"User request: {user_query}\n\n"
        f"Use skill '{skill}' and produce decision-grade output. "
        f"Must include sections: {req}.\n\n"
        f"Deal Context:\n```json\n{deal_json}\n```"
    )
    response = harness.run(prompt, skill=skill)
    output = str(getattr(response, "content", response))
    return output, _contains_sections(output, required_sections)


def _live_run_for_deal(
    deal_file: Path,
    rubric: dict[str, Any],
    *,
    rubric_file: Path,
    model: str,
    workspace_dir: Path | None,
    user_query: str,
    enable_scoring: bool,
) -> dict[str, Any]:
    deal = _load_json(deal_file)
    sink = InMemoryEventSink()

    if workspace_dir is None:
        tmp = tempfile.TemporaryDirectory(prefix="pe-live-workspace-")
        ws_path = Path(tmp.name)
    else:
        tmp = None
        ws_path = workspace_dir

    try:
        _write_skills_for_deal(ws_path / "skills", deal_file=deal_file, rubric_file=rubric_file)

        harness = AgentHarness(
            model=model,
            workspace_dir=ws_path,
            event_sink=sink,
        )

        selected_skills = _route_skills(user_query, enable_scoring=enable_scoring)
        deal_json = json.dumps(deal, indent=2)

        skill_outputs: dict[str, str] = {}
        section_checks: dict[str, dict[str, bool]] = {}

        for skill in selected_skills:
            if skill == "pe-due-diligence-lifecycle":
                required = rubric["output_contract"]["due_diligence_required_sections"]
            else:
                required = rubric["output_contract"]["deal_scoring_required_sections"]

            output, sections = _run_skill_with_contract(
                harness,
                skill=skill,
                deal_json=deal_json,
                required_sections=required,
                user_query=user_query,
            )
            skill_outputs[skill] = output
            section_checks[skill] = sections

        event_types = [evt.event_type for evt in sink.events]
        all_sections_present = all(all(sections.values()) for sections in section_checks.values())

        return {
            "deal_file": str(deal_file),
            "deal_id": deal.get("deal_id"),
            "live_mode": {
                "model": model,
                "selected_skills": selected_skills,
                "section_checks": section_checks,
                "all_sections_present": all_sections_present,
                "event_counts": {
                    "run.started": event_types.count("run.started"),
                    "skill.load.started": event_types.count("skill.load.started"),
                    "skill.load.completed": event_types.count("skill.load.completed"),
                    "run.completed": event_types.count("run.completed"),
                },
                "outputs_preview": {name: text[:500] for name, text in skill_outputs.items()},
            },
        }
    finally:
        if tmp is not None:
            tmp.cleanup()


def _attachment_deal_file(
    args: argparse.Namespace,
) -> tuple[Path | None, dict[str, Any] | None]:
    if not args.attachment:
        return None, None

    deal_name = args.deal_name or Path(args.attachment[0]).stem
    deal_id = args.deal_id or f"PE-REAL-{deal_name[:24].upper().replace(' ', '-')}"

    pack, report = build_pack_from_attachments(
        attachments=[Path(p) for p in args.attachment],
        deal_id=deal_id,
        deal_name=deal_name,
        sector=args.sector,
        sponsor=args.sponsor,
        strategy=args.strategy,
    )

    with tempfile.NamedTemporaryFile(
        mode="w",
        prefix="pe-attachment-pack-",
        suffix=".json",
        delete=False,
        encoding="utf-8",
    ) as fh:
        json.dump(pack, fh, indent=2)
        path = Path(fh.name)

    return path, report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pe-risk-simulate",
        description="Embedded-library PE risk workflow simulation runner.",
    )
    parser.add_argument(
        "--mode",
        choices=["contract", "live", "both"],
        default="contract",
        help="contract: deterministic checks, live: model calls, both: run both",
    )
    parser.add_argument(
        "--deals-dir",
        default="",
        help="Directory containing deal_pack_v1 JSON files",
    )
    parser.add_argument(
        "--use-fixtures",
        action="store_true",
        help="Include bundled simulated fixtures from examples/pe_risk_platform/fixtures/deals",
    )
    parser.add_argument(
        "--deal-file",
        action="append",
        default=[],
        help="Run one-off deal file (repeatable).",
    )
    parser.add_argument(
        "--attachment",
        action="append",
        default=[],
        help="Attachment path(s) for chat-like ingestion (repeatable).",
    )
    parser.add_argument("--deal-id", default="", help="Deal ID for attachment mode")
    parser.add_argument("--deal-name", default="", help="Deal name for attachment mode")
    parser.add_argument("--sector", default="Unknown", help="Sector for attachment mode")
    parser.add_argument("--sponsor", default="Agnoclaw Capital Partners")
    parser.add_argument("--strategy", default="Control buyout")
    parser.add_argument(
        "--rubric",
        default=str(DEFAULT_RUBRIC_PATH),
        help="Rubric JSON path",
    )
    parser.add_argument(
        "--model",
        default="",
        help="Model string for live mode, e.g. anthropic:claude-sonnet-4-6",
    )
    parser.add_argument(
        "--workspace-dir",
        default="",
        help="Optional persistent workspace for live mode",
    )
    parser.add_argument(
        "--query",
        default="Review this deal for downside risk and provide IC-ready recommendations.",
        help="User request used for live skill routing/execution.",
    )
    parser.add_argument("--min-completeness", type=float, default=0.85)
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument(
        "--fail-on-errors",
        action="store_true",
        help="Return non-zero if any quality/contract/live check fails.",
    )
    parser.add_argument(
        "--save-report",
        default="",
        help="Optional path to write full JSON report.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    deals_dir = args.deals_dir
    if args.use_fixtures and not deals_dir:
        deals_dir = str(ROOT / "fixtures" / "deals")

    deal_files = _collect_deal_files(deals_dir, args.deal_file)
    attachment_file, attachment_report = _attachment_deal_file(args)
    if attachment_file is not None:
        deal_files.append(attachment_file)

    if not deal_files:
        print("error: no deal files found", file=sys.stderr)
        return 3

    rubric_file = Path(args.rubric).resolve()
    rubric = _load_json(rubric_file)

    mode = args.mode
    run_contract = mode in ("contract", "both")
    run_live = mode in ("live", "both")

    model = args.model
    if run_live and not model:
        if detect_model is None:
            print("error: --model is required (detect_model unavailable)", file=sys.stderr)
            return 2
        model = detect_model()

    workspace_dir = Path(args.workspace_dir).resolve() if args.workspace_dir else None

    records: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for deal_file in deal_files:
        entry: dict[str, Any] = {"deal_file": str(deal_file)}

        deal_payload = _load_json(deal_file)
        gate = quality_gate(
            deal_payload,
            min_overall_coverage=args.min_completeness,
            require_critical_complete=not args.allow_partial,
        )
        entry["quality_gate"] = gate

        if not gate["passed"] and not args.allow_partial:
            failures.append({"deal_file": str(deal_file), "mode": "quality_gate"})
            records.append(entry)
            continue

        enable_scoring = gate["completeness"]["critical_coverage"] >= 1.0

        if run_contract:
            contract_record = _contract_check_for_deal(
                deal_file,
                rubric,
                rubric_file=rubric_file,
            )
            entry.update(contract_record)
            contract_fail = not (
                contract_record["contract_mode"]["fixture_validation"]["all_passed"]
                and contract_record["contract_mode"]["all_sections_present"]
                and contract_record["contract_mode"]["score_snapshot_in_skill_render"]
            )
            if contract_fail:
                failures.append({"deal_file": str(deal_file), "mode": "contract"})

        if run_live:
            live_record = _live_run_for_deal(
                deal_file,
                rubric,
                rubric_file=rubric_file,
                model=model,
                workspace_dir=workspace_dir,
                user_query=args.query,
                enable_scoring=enable_scoring,
            )
            entry.update(live_record)
            if not live_record["live_mode"]["all_sections_present"]:
                failures.append({"deal_file": str(deal_file), "mode": "live"})

        records.append(entry)

    payload = {
        "status": "ok" if not failures else "error",
        "data": {
            "mode": mode,
            "rubric": str(rubric_file),
            "deals_evaluated": len(records),
            "failures": failures,
            "records": records,
            "attachment_report": attachment_report,
        },
        "warnings": [],
    }

    if args.save_report:
        Path(args.save_report).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    if args.format == "json":
        print(json.dumps(payload, indent=2))
    else:
        print(f"Mode={mode} deals={len(records)} failures={len(failures)}")
        for record in records:
            deal_id = record.get("deal_id", "unknown")
            q_pass = record["quality_gate"]["passed"]
            coverage = record["quality_gate"]["completeness"]["overall_coverage"]
            print(f"- {deal_id} quality_pass={q_pass} coverage={coverage}")
            if "contract_mode" in record:
                c = record["contract_mode"]
                print(
                    "  contract:"
                    f" fixture={c['fixture_validation']['all_passed']}"
                    f" sections={c['all_sections_present']}"
                    f" score_snapshot={c['score_snapshot_in_skill_render']}"
                )
            if "live_mode" in record:
                live_mode = record["live_mode"]
                print(
                    "  live:"
                    f" sections={live_mode['all_sections_present']}"
                    f" selected={','.join(live_mode['selected_skills'])}"
                    f" run_completed={live_mode['event_counts']['run.completed']}"
                )

    if args.fail_on_errors and failures:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
