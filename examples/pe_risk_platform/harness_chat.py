"""Interactive PE risk chat powered by AgentHarness.

Run:
  uv run python examples/pe_risk_platform/harness_chat.py

This gives a chat workflow with live commands for:
- attaching deal documents
- building normalized deal packs
- tuning rubric weights
- running deterministic scoring
- running skill-driven IC analysis with AgentHarness
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agnoclaw import AgentHarness
from agnoclaw.runtime import HarnessEvent

try:
    from examples._utils import detect_model
except Exception:  # pragma: no cover
    detect_model = None

try:  # package import
    from .deal_pack_builder import build_pack_from_attachments
    from .quality_gates import quality_gate
    from .rubric_tools import load_rubric, save_rubric, set_weight_renormalized
    from .scoring_engine import DEFAULT_RUBRIC_PATH, score_deal
    from .simulate_embedding import _route_skills, _write_skills_for_deal
except Exception:  # pragma: no cover - script fallback
    from deal_pack_builder import build_pack_from_attachments
    from quality_gates import quality_gate
    from rubric_tools import load_rubric, save_rubric, set_weight_renormalized
    from scoring_engine import DEFAULT_RUBRIC_PATH, score_deal
    from simulate_embedding import _route_skills, _write_skills_for_deal


@dataclass
class ChatState:
    deal_id: str = "PE-REAL-001"
    deal_name: str = "Project Atlas"
    sector: str = "Unknown"
    sponsor: str = "Agnoclaw Capital Partners"
    strategy: str = "Control buyout"
    attachments: list[Path] = field(default_factory=list)
    deal_pack_path: Path | None = None
    allow_partial: bool = False


class VerboseEventSink:
    """Optional event sink that prints and/or persists harness events."""

    def __init__(
        self,
        *,
        show_events: bool,
        show_event_payloads: bool,
        events_file: Path | None,
    ) -> None:
        self._show_events = show_events
        self._show_event_payloads = show_event_payloads
        self._events_file = events_file
        if self._events_file is not None:
            self._events_file.parent.mkdir(parents=True, exist_ok=True)
            self._events_file.write_text("", encoding="utf-8")

    def emit(self, event: HarnessEvent) -> None:
        event_doc = event.to_dict()

        if self._events_file is not None:
            with self._events_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event_doc, ensure_ascii=True, default=str))
                fh.write("\n")

        if not self._show_events:
            return

        print(
            f"[event] {event.occurred_at} {event.event_type} run_id={event.run_id}"
        )
        if self._show_event_payloads:
            payload = json.dumps(
                event.payload,
                ensure_ascii=True,
                default=str,
                sort_keys=True,
            )
            print(f"        payload={payload}")


def _print_help() -> None:
    print(
        "Commands:\n"
        "  /help\n"
        "  /quit\n"
        "  /attach <path>\n"
        "  /attachments\n"
        "  /clear-attachments\n"
        "  /deal-id <id>\n"
        "  /deal-name <name>\n"
        "  /sector <sector>\n"
        "  /sponsor <name>\n"
        "  /strategy <value>\n"
        "  /allow-partial on|off\n"
        "  /build              (build deal_pack_v1 from attachments)\n"
        "  /score              (deterministic score on built pack)\n"
        "  /weights            (show current rubric weights)\n"
        "  /weight <dimension> <0..1>\n"
        "\n"
        "Any non-slash input is treated as a user query and analyzed via AgentHarness."
    )


def _ensure_rubric_copy(workspace_dir: Path, source_rubric: Path) -> Path:
    rubric_dir = workspace_dir / "rubrics"
    rubric_path = rubric_dir / "active_rubric.json"
    if not rubric_path.exists():
        rubric = load_rubric(source_rubric)
        save_rubric(rubric_path, rubric)
    return rubric_path


def _build_pack(
    state: ChatState, *, rubric_path: Path, workspace_dir: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not state.attachments:
        raise ValueError("No attachments. Use /attach <path> first.")

    pack, ingestion = build_pack_from_attachments(
        attachments=state.attachments,
        deal_id=state.deal_id,
        deal_name=state.deal_name,
        sector=state.sector,
        sponsor=state.sponsor,
        strategy=state.strategy,
    )

    gate = quality_gate(
        pack,
        min_overall_coverage=0.85,
        require_critical_complete=not state.allow_partial,
    )

    deals_dir = workspace_dir / "deals"
    deals_dir.mkdir(parents=True, exist_ok=True)
    state.deal_pack_path = deals_dir / f"{pack['deal_id']}.json"
    state.deal_pack_path.write_text(json.dumps(pack, indent=2), encoding="utf-8")

    return ingestion, gate


def _print_weights(rubric: dict[str, Any]) -> None:
    print("Current weights:")
    weights = rubric.get("weights") or {}
    for k, v in weights.items():
        print(f"  {k}: {float(v):.4f}")
    print(f"  total: {sum(float(v) for v in weights.values()):.4f}")


def _handle_command(
    line: str,
    *,
    state: ChatState,
    workspace_dir: Path,
    rubric_path: Path,
) -> bool:
    parts = line.strip().split(" ", 1)
    cmd = parts[0].lower()
    args = parts[1].strip() if len(parts) > 1 else ""

    if cmd in {"/quit", "/exit"}:
        return True

    if cmd == "/help":
        _print_help()
        return False

    if cmd == "/attach":
        if not args:
            print("usage: /attach <path>")
            return False
        p = Path(args).expanduser().resolve()
        if not p.exists():
            print(f"error: file not found: {p}")
            return False
        state.attachments.append(p)
        print(f"attached: {p}")
        return False

    if cmd == "/attachments":
        if not state.attachments:
            print("no attachments")
        else:
            print("attachments:")
            for a in state.attachments:
                print(f"  - {a}")
        return False

    if cmd == "/clear-attachments":
        state.attachments.clear()
        state.deal_pack_path = None
        print("attachments cleared")
        return False

    if cmd == "/deal-id":
        state.deal_id = args
        print(f"deal_id={state.deal_id}")
        return False

    if cmd == "/deal-name":
        state.deal_name = args
        print(f"deal_name={state.deal_name}")
        return False

    if cmd == "/sector":
        state.sector = args
        print(f"sector={state.sector}")
        return False

    if cmd == "/sponsor":
        state.sponsor = args
        print(f"sponsor={state.sponsor}")
        return False

    if cmd == "/strategy":
        state.strategy = args
        print(f"strategy={state.strategy}")
        return False

    if cmd == "/allow-partial":
        state.allow_partial = args.lower() in {"1", "true", "on", "yes"}
        print(f"allow_partial={state.allow_partial}")
        return False

    if cmd == "/build":
        ingestion, gate = _build_pack(state, rubric_path=rubric_path, workspace_dir=workspace_dir)
        print(f"built deal pack: {state.deal_pack_path}")
        print(
            "quality_gate:"
            f" passed={gate['passed']}"
            f" coverage={gate['completeness']['overall_coverage']}"
            f" critical_missing={len(gate['completeness']['missing_critical_fields'])}"
        )
        if ingestion.get("warnings"):
            print("warnings:")
            for w in ingestion["warnings"]:
                print(f"  - {w}")
        return False

    if cmd == "/score":
        if not state.deal_pack_path or not state.deal_pack_path.exists():
            print("error: no built pack. run /build first")
            return False
        deal = json.loads(state.deal_pack_path.read_text(encoding="utf-8"))
        rubric = load_rubric(rubric_path)
        result = score_deal(deal, rubric)
        print(
            f"score={result['composite_risk_score']} band={result['risk_band']} "
            f"action={result['recommended_action']}"
        )
        return False

    if cmd == "/weights":
        rubric = load_rubric(rubric_path)
        _print_weights(rubric)
        return False

    if cmd == "/weight":
        parts = args.split()
        if len(parts) != 2:
            print("usage: /weight <dimension> <0..1>")
            return False
        dim = parts[0]
        try:
            val = float(parts[1])
        except ValueError:
            print("error: weight must be numeric")
            return False

        rubric = load_rubric(rubric_path)
        updated = set_weight_renormalized(rubric, dim, val)
        save_rubric(rubric_path, updated)
        print(f"updated weight: {dim}={val:.4f}")
        _print_weights(updated)
        return False

    print(f"unknown command: {cmd}. try /help")
    return False


def run_chat(
    model: str,
    workspace_dir: Path,
    source_rubric: Path,
    *,
    show_events: bool = False,
    show_event_payloads: bool = False,
    events_file: Path | None = None,
) -> int:
    state = ChatState()
    workspace_dir.mkdir(parents=True, exist_ok=True)
    rubric_path = _ensure_rubric_copy(workspace_dir, source_rubric)

    event_sink = VerboseEventSink(
        show_events=show_events,
        show_event_payloads=show_event_payloads,
        events_file=events_file,
    ) if (show_events or events_file is not None) else None

    harness_kwargs: dict[str, Any] = {
        "model": model,
        "workspace_dir": workspace_dir,
    }
    if event_sink is not None:
        harness_kwargs["event_sink"] = event_sink
    harness = AgentHarness(**harness_kwargs)

    stream_chat_runs = event_sink is not None

    print(f"PE risk chat started. model={model}")
    print(f"workspace={workspace_dir}")
    print(f"rubric={rubric_path}")
    if show_events:
        print("event_stream=enabled")
    if events_file is not None:
        print(f"events_file={events_file}")
    _print_help()

    while True:
        try:
            line = input("\n[you] > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            return 0

        if not line:
            continue

        if line.startswith("/"):
            should_quit = _handle_command(
                line,
                state=state,
                workspace_dir=workspace_dir,
                rubric_path=rubric_path,
            )
            if should_quit:
                print("bye")
                return 0
            continue

        try:
            if not state.deal_pack_path or not state.deal_pack_path.exists():
                _build_pack(state, rubric_path=rubric_path, workspace_dir=workspace_dir)

            deal = json.loads(state.deal_pack_path.read_text(encoding="utf-8"))
            gate = quality_gate(
                deal,
                min_overall_coverage=0.85,
                require_critical_complete=not state.allow_partial,
            )
            if not gate["passed"] and not state.allow_partial:
                print("quality gate failed. run /build and inspect missing fields first.")
                print(gate["errors"])
                continue

            _write_skills_for_deal(
                workspace_dir / "skills",
                deal_file=state.deal_pack_path,
                rubric_file=rubric_path,
            )

            enable_scoring = gate["completeness"]["critical_coverage"] >= 1.0
            selected = _route_skills(line, enable_scoring=enable_scoring)

            deal_json = json.dumps(deal, indent=2)
            for skill in selected:
                prompt = (
                    f"User request: {line}\n\n"
                    f"Use skill '{skill}' for this deal.\n\n"
                    f"Deal Context:\n```json\n{deal_json}\n```"
                )
                print(f"\n[agent:{skill}]")

                if stream_chat_runs:
                    stream = harness.run(
                        prompt,
                        skill=skill,
                        stream=True,
                        stream_events=True,
                    )
                    for event in stream:
                        content = harness._extract_event_content(event)
                        if content:
                            print(content, end="", flush=True)
                    print("\n")
                else:
                    result = harness.run(prompt, skill=skill)
                    content = str(getattr(result, "content", result))
                    print(f"{content}\n")

        except Exception as exc:
            print(f"error: {exc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Interactive PE risk chat (AgentHarness-backed)")
    parser.add_argument("--model", default="", help="e.g. anthropic:claude-sonnet-4-6")
    parser.add_argument(
        "--workspace",
        default=str(Path.home() / ".agnoclaw" / "pe-risk-chat"),
        help="Workspace directory for chat session",
    )
    parser.add_argument(
        "--rubric",
        default=str(DEFAULT_RUBRIC_PATH),
        help="Base rubric path",
    )
    parser.add_argument(
        "--show-events",
        action="store_true",
        help="Print all emitted harness events during each run.",
    )
    parser.add_argument(
        "--show-event-payloads",
        action="store_true",
        help="Include event payload JSON in printed event logs.",
    )
    parser.add_argument(
        "--events-file",
        default="",
        help="Optional NDJSON file path to persist all emitted events.",
    )

    args = parser.parse_args(argv)

    model = args.model
    if not model:
        if detect_model is None:
            print("error: --model required")
            return 2
        model = detect_model()

    events_file = (
        Path(args.events_file).expanduser().resolve()
        if args.events_file
        else None
    )

    return run_chat(
        model=model,
        workspace_dir=Path(args.workspace).expanduser().resolve(),
        source_rubric=Path(args.rubric).expanduser().resolve(),
        show_events=args.show_events,
        show_event_payloads=args.show_event_payloads,
        events_file=events_file,
    )


if __name__ == "__main__":
    raise SystemExit(main())
