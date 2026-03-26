"""Build normalized PE deal packs from JSON/PPTX/attachment source materials.

Examples:
  uv run python examples/pe_risk_platform/deal_pack_builder.py from-json \
    --input raw_deal.json --output /tmp/PE-REAL-001.json

  uv run python examples/pe_risk_platform/deal_pack_builder.py from-pptx \
    --input deck.pptx --deal-id PE-REAL-002 --deal-name "Project Atlas" \
    --sector "Healthcare Services" --output /tmp/PE-REAL-002.json

  uv run python examples/pe_risk_platform/deal_pack_builder.py from-attachments \
    --input deck.pptx --input qoe_notes.docx --input financials.xlsx \
    --deal-id PE-REAL-003 --deal-name "Project Orion" --sector "Industrials" \
    --output /tmp/PE-REAL-003.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from html import unescape
from pathlib import Path
from typing import Any

try:  # package import
    from .quality_gates import REQUIRED_METRICS, quality_gate
except Exception:  # pragma: no cover - script execution fallback
    from quality_gates import REQUIRED_METRICS, quality_gate


def _empty_metrics() -> dict[str, float | None]:
    return {field: None for field in REQUIRED_METRICS}


def _extract_pptx_lines(path: Path) -> list[str]:
    lines: list[str] = []
    with zipfile.ZipFile(path) as archive:
        slide_paths = sorted(
            [
                name
                for name in archive.namelist()
                if name.startswith("ppt/slides/slide") and name.endswith(".xml")
            ],
            key=lambda p: (
                int(re.search(r"slide(\d+)\.xml", p).group(1))
                if re.search(r"slide(\d+)\.xml", p)
                else 0
            ),
        )
        for slide in slide_paths:
            raw = archive.read(slide).decode("utf-8", errors="ignore")
            text_runs = re.findall(r"<a:t>(.*?)</a:t>", raw)
            for run in text_runs:
                line = unescape(run).strip()
                if line:
                    lines.append(line)
    return lines


def _extract_docx_lines(path: Path) -> list[str]:
    lines: list[str] = []
    with zipfile.ZipFile(path) as archive:
        if "word/document.xml" not in archive.namelist():
            return lines
        raw = archive.read("word/document.xml").decode("utf-8", errors="ignore")
        runs = re.findall(r"<w:t[^>]*>(.*?)</w:t>", raw)
        for run in runs:
            line = unescape(run).strip()
            if line:
                lines.append(line)
    return lines


def _extract_xlsx_lines(path: Path) -> list[str]:
    lines: list[str] = []
    with zipfile.ZipFile(path) as archive:
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            raw = archive.read("xl/sharedStrings.xml").decode("utf-8", errors="ignore")
            shared_strings = [unescape(t) for t in re.findall(r"<t[^>]*>(.*?)</t>", raw)]

        sheet_paths = sorted(
            [
                name
                for name in archive.namelist()
                if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
            ]
        )

        for sheet in sheet_paths:
            raw = archive.read(sheet).decode("utf-8", errors="ignore")
            for cell in re.findall(r"<c[^>]*>.*?</c>", raw, flags=re.DOTALL):
                typ_match = re.search(r't="(.*?)"', cell)
                val_match = re.search(r"<v>(.*?)</v>", cell)
                if not val_match:
                    continue
                value = val_match.group(1).strip()
                if not value:
                    continue

                if typ_match and typ_match.group(1) == "s":
                    try:
                        idx = int(value)
                        if 0 <= idx < len(shared_strings):
                            lines.append(shared_strings[idx])
                    except ValueError:
                        continue
                else:
                    lines.append(value)

    return lines


def _extract_pdf_lines(path: Path) -> tuple[list[str], list[str]]:
    warnings: list[str] = []
    lines: list[str] = []

    try:
        import fitz  # type: ignore
    except Exception:
        warnings.append("PDF parsing skipped: pymupdf not installed")
        return lines, warnings

    try:
        with fitz.open(path) as doc:
            for page in doc:
                text = page.get_text("text")
                for line in text.splitlines():
                    stripped = line.strip()
                    if stripped:
                        lines.append(stripped)
    except Exception as exc:
        warnings.append(f"PDF parsing failed: {exc}")

    return lines, warnings


def _extract_text_lines(path: Path) -> tuple[list[str], list[str]]:
    suffix = path.suffix.lower()

    if suffix == ".pptx":
        return _extract_pptx_lines(path), []
    if suffix == ".docx":
        return _extract_docx_lines(path), []
    if suffix == ".xlsx":
        return _extract_xlsx_lines(path), []
    if suffix == ".pdf":
        return _extract_pdf_lines(path)
    if suffix in {".txt", ".md", ".csv", ".tsv", ".log"}:
        text = path.read_text(encoding="utf-8", errors="ignore")
        return [line.strip() for line in text.splitlines() if line.strip()], []

    return [], [f"Unsupported file type ignored: {path.name}"]


def _extract_first_float(patterns: list[str], text: str) -> float | None:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            try:
                return float(match.group(1).replace(",", ""))
            except ValueError:
                continue
    return None


def _parse_metrics_from_text(
    text_lines: list[str],
) -> tuple[dict[str, float | None], dict[str, Any]]:
    text_blob = "\n".join(text_lines)
    metrics: dict[str, float | None] = _empty_metrics()

    extracted = {
        "total_leverage_x": _extract_first_float(
            [
                r"(?:total\s+)?leverage[^0-9]{0,16}([0-9]+(?:\.[0-9]+)?)\s*x",
                r"net\s+debt\s*/\s*ebitda[^0-9]{0,16}([0-9]+(?:\.[0-9]+)?)\s*x",
            ],
            text_blob,
        ),
        "interest_coverage_x": _extract_first_float(
            [r"interest\s+coverage[^0-9]{0,16}([0-9]+(?:\.[0-9]+)?)\s*x"],
            text_blob,
        ),
        "cash_interest_rate_pct": _extract_first_float(
            [r"(?:cash\s+)?interest\s+rate[^0-9]{0,16}([0-9]+(?:\.[0-9]+)?)\s*%"],
            text_blob,
        ),
        "covenant_headroom_x": _extract_first_float(
            [r"covenant\s+headroom[^0-9]{0,16}([0-9]+(?:\.[0-9]+)?)\s*x"],
            text_blob,
        ),
        "recurring_revenue_pct": _extract_first_float(
            [r"recurring\s+revenue[^0-9]{0,16}([0-9]+(?:\.[0-9]+)?)\s*%"],
            text_blob,
        ),
        "net_revenue_retention_pct": _extract_first_float(
            [r"(?:nrr|net\s+revenue\s+retention)[^0-9]{0,16}([0-9]+(?:\.[0-9]+)?)\s*%"],
            text_blob,
        ),
        "top_customer_pct": _extract_first_float(
            [r"top\s+customer[^0-9]{0,16}([0-9]+(?:\.[0-9]+)?)\s*%"],
            text_blob,
        ),
        "customer_churn_pct": _extract_first_float(
            [r"churn[^0-9]{0,16}([0-9]+(?:\.[0-9]+)?)\s*%"],
            text_blob,
        ),
        "free_cash_flow_conversion_pct": _extract_first_float(
            [r"(?:fcf|cash)\s+conversion[^0-9]{0,16}([0-9]+(?:\.[0-9]+)?)\s*%"],
            text_blob,
        ),
        "qoe_adjustments_pct_ebitda": _extract_first_float(
            [r"qoe\s+adjustments?[^0-9]{0,16}([0-9]+(?:\.[0-9]+)?)\s*%"],
            text_blob,
        ),
    }

    for field in REQUIRED_METRICS:
        if field in extracted and extracted[field] is not None:
            metrics[field] = extracted[field]

    return metrics, extracted


def _infer_flags(text_lines: list[str]) -> list[dict[str, str]]:
    flags: list[dict[str, str]] = []
    blob = " ".join(text_lines).lower()

    if any(token in blob for token in ["investigation", "subpoena", "doj", "sec inquiry"]):
        flags.append(
            {
                "id": "AUTO-REG-01",
                "severity": "high",
                "dimension": "legal_regulatory",
                "description": "Potential regulatory investigation language detected.",
            }
        )

    if any(token in blob for token in ["single supplier", "sole supplier", "single-source"]):
        flags.append(
            {
                "id": "AUTO-OPS-01",
                "severity": "medium",
                "dimension": "market_operational",
                "description": "Potential supplier concentration signal detected.",
            }
        )

    if any(token in blob for token in ["customer concentration", "top customer"]):
        flags.append(
            {
                "id": "AUTO-CUST-01",
                "severity": "medium",
                "dimension": "revenue_quality",
                "description": "Customer concentration signal detected from source docs.",
            }
        )

    return flags


def _merge_dict(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge_dict(out[key], value)
        else:
            out[key] = value
    return out


def _deal_id_from_name(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name.strip()).strip("-").upper()
    return f"PE-REAL-{slug[:24] or 'UNKNOWN'}"


def build_pack_from_attachments(
    *,
    attachments: list[Path],
    deal_id: str,
    deal_name: str,
    sector: str,
    sponsor: str,
    strategy: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    all_lines: list[str] = []
    warnings: list[str] = []
    merged_json: dict[str, Any] = {}
    source_entries: list[dict[str, Any]] = []

    for path in attachments:
        path = path.resolve()
        suffix = path.suffix.lower()

        if suffix == ".json":
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                merged_json = _merge_dict(merged_json, payload)
                source_entries.append(
                    {
                        "path": str(path),
                        "type": "json",
                        "line_count": 0,
                    }
                )
                all_lines.append(json.dumps(payload))
                continue

        lines, file_warnings = _extract_text_lines(path)
        warnings.extend(file_warnings)
        all_lines.extend(lines)
        source_entries.append(
            {
                "path": str(path),
                "type": suffix.lstrip(".") or "unknown",
                "line_count": len(lines),
            }
        )

    metrics, extracted = _parse_metrics_from_text(all_lines)
    auto_flags = _infer_flags(all_lines)

    debt = {
        "total_leverage_x": extracted.get("total_leverage_x"),
        "cash_interest_rate_pct": extracted.get("cash_interest_rate_pct"),
        "minimum_interest_coverage_x": extracted.get("interest_coverage_x"),
        "covenant_headroom_x": extracted.get("covenant_headroom_x"),
    }

    base_pack: dict[str, Any] = {
        "deal_id": deal_id,
        "deal_name": deal_name,
        "sponsor": sponsor,
        "strategy": strategy,
        "sector": sector,
        "investment_thesis": "Imported from attached documents. Refine with diligence findings.",
        "transaction": {
            "enterprise_value_usd_mn": None,
            "entry_ebitda_multiple": None,
            "equity_check_usd_mn": None,
            "debt_package": debt,
        },
        "metrics": metrics,
        "diligence_flags": auto_flags,
        "open_diligence_questions": [
            "Validate management base-case and downside-case assumptions line by line.",
            "Confirm legal and compliance exposure with outside counsel diligence memo.",
            "Reconcile QoE adjustments to audited financial statements.",
        ],
        "source_material": {
            "type": "mixed"
            if len(attachments) > 1
            else (attachments[0].suffix.lstrip(".") if attachments else "unknown"),
            "attachments": source_entries,
            "line_count": len(all_lines),
            "extracted_fields": extracted,
            "sample_lines": all_lines[:60],
        },
    }

    pack = _merge_dict(base_pack, merged_json)
    report = {
        "warnings": warnings,
        "attachments": [str(p) for p in attachments],
        "extracted_fields": extracted,
    }
    return pack, report


def _quality_result(
    pack: dict[str, Any],
    *,
    min_completeness: float,
    allow_partial: bool,
) -> tuple[bool, dict[str, Any]]:
    gate = quality_gate(
        pack,
        min_overall_coverage=min_completeness,
        require_critical_complete=not allow_partial,
    )
    return gate["passed"] or allow_partial, gate


def _cmd_from_json(args: argparse.Namespace) -> int:
    source = Path(args.input)
    payload = json.loads(source.read_text(encoding="utf-8"))

    ok, gate = _quality_result(
        payload,
        min_completeness=args.min_completeness,
        allow_partial=args.allow_partial,
    )
    Path(args.output).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    status = "ok" if ok else "error"
    print(
        json.dumps(
            {
                "status": status,
                "data": {"output": str(args.output), "quality_gate": gate},
                "warnings": [],
            },
            indent=2,
        )
    )
    return 0 if ok else 1


def _cmd_from_pptx(args: argparse.Namespace) -> int:
    return _cmd_from_attachments(
        argparse.Namespace(
            input=[args.input],
            output=args.output,
            deal_id=args.deal_id,
            deal_name=args.deal_name,
            sector=args.sector,
            sponsor=args.sponsor,
            strategy=args.strategy,
            min_completeness=args.min_completeness,
            allow_partial=args.allow_partial,
        )
    )


def _cmd_from_attachments(args: argparse.Namespace) -> int:
    attachments = [Path(p) for p in args.input]
    if not attachments:
        print(
            json.dumps({"status": "error", "error": {"message": "No attachments provided"}}),
            file=sys.stderr,
        )
        return 2

    deal_name = args.deal_name or attachments[0].stem
    deal_id = args.deal_id or _deal_id_from_name(deal_name)

    pack, ingestion_report = build_pack_from_attachments(
        attachments=attachments,
        deal_id=deal_id,
        deal_name=deal_name,
        sector=args.sector,
        sponsor=args.sponsor,
        strategy=args.strategy,
    )

    ok, gate = _quality_result(
        pack,
        min_completeness=args.min_completeness,
        allow_partial=args.allow_partial,
    )

    out = Path(args.output)
    out.write_text(json.dumps(pack, indent=2), encoding="utf-8")
    status = "ok" if ok else "error"

    print(
        json.dumps(
            {
                "status": status,
                "data": {
                    "output": str(out),
                    "quality_gate": gate,
                    "ingestion_report": ingestion_report,
                },
                "warnings": ingestion_report["warnings"],
            },
            indent=2,
        )
    )
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deal-pack-builder",
        description="Normalize external deal materials into a PE deal-pack JSON schema.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    from_json = sub.add_parser("from-json", help="Pass-through or normalize from existing JSON")
    from_json.add_argument("--input", required=True, help="Input JSON path")
    from_json.add_argument("--output", required=True, help="Output deal-pack JSON path")
    from_json.add_argument("--min-completeness", type=float, default=0.85)
    from_json.add_argument("--allow-partial", action="store_true")
    from_json.set_defaults(func=_cmd_from_json)

    from_pptx = sub.add_parser("from-pptx", help="Extract text from PPTX and build deal-pack JSON")
    from_pptx.add_argument("--input", required=True, help="Input .pptx path")
    from_pptx.add_argument("--output", required=True, help="Output deal-pack JSON path")
    from_pptx.add_argument("--deal-id", required=False, default="", help="Deal ID")
    from_pptx.add_argument("--deal-name", required=False, default="", help="Deal name")
    from_pptx.add_argument("--sector", required=True, help="Sector label")
    from_pptx.add_argument("--sponsor", default="Agnoclaw Capital Partners", help="Sponsor name")
    from_pptx.add_argument("--strategy", default="Control buyout", help="Investment strategy")
    from_pptx.add_argument("--min-completeness", type=float, default=0.85)
    from_pptx.add_argument("--allow-partial", action="store_true")
    from_pptx.set_defaults(func=_cmd_from_pptx)

    from_attachments = sub.add_parser(
        "from-attachments",
        help="Build deal pack from multiple attachments (pptx/docx/xlsx/txt/pdf/json)",
    )
    from_attachments.add_argument(
        "--input",
        action="append",
        required=True,
        help="Attachment path (repeatable)",
    )
    from_attachments.add_argument("--output", required=True, help="Output deal-pack JSON path")
    from_attachments.add_argument("--deal-id", default="", help="Deal ID")
    from_attachments.add_argument("--deal-name", default="", help="Deal name")
    from_attachments.add_argument("--sector", required=True, help="Sector label")
    from_attachments.add_argument(
        "--sponsor", default="Agnoclaw Capital Partners", help="Sponsor name"
    )
    from_attachments.add_argument(
        "--strategy", default="Control buyout", help="Investment strategy"
    )
    from_attachments.add_argument("--min-completeness", type=float, default=0.85)
    from_attachments.add_argument("--allow-partial", action="store_true")
    from_attachments.set_defaults(func=_cmd_from_attachments)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except FileNotFoundError as exc:
        print(json.dumps({"status": "error", "error": {"message": str(exc)}}), file=sys.stderr)
        return 3
    except zipfile.BadZipFile as exc:
        print(
            json.dumps(
                {"status": "error", "error": {"message": f"Invalid Office/PPTX file: {exc}"}}
            ),
            file=sys.stderr,
        )
        return 2
    except json.JSONDecodeError as exc:
        print(
            json.dumps({"status": "error", "error": {"message": f"Invalid JSON: {exc}"}}),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
