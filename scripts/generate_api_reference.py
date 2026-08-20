#!/usr/bin/env python3
"""Generate the complete top-level agnoclaw API reference deterministically."""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import agnoclaw

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "docs" / "reference" / "api.md"
SCHEMA_VERSION = "1.0"
_ADDRESS_RE = re.compile(r" at 0x[0-9a-fA-F]+")


def _normalize_runtime_repr(value: str) -> str:
    """Remove process-specific addresses without hiding the referenced object."""
    return _ADDRESS_RE.sub("", value)


def _split_signature(value: str) -> tuple[list[str], str] | None:
    if not value.startswith("("):
        return None
    stack: list[str] = []
    quote: str | None = None
    escaped = False
    parts: list[str] = []
    start = 1
    closing = -1
    pairs = {")": "(", "]": "[", "}": "{"}
    for index, character in enumerate(value[1:], start=1):
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in {"'", '"'}:
            quote = character
        elif character in "([{":
            stack.append(character)
        elif character in ")]}" and stack:
            if stack[-1] != pairs[character]:
                return None
            stack.pop()
        elif character == ")" and not stack:
            closing = index
            break
        elif character == "," and not stack:
            parts.append(value[start:index].strip())
            start = index + 1
    if closing < 0:
        return None
    final = value[start:closing].strip()
    if final:
        parts.append(final)
    return parts, value[closing + 1 :]


def _format_signature(value: str, *, width: int = 96) -> str:
    value = _normalize_runtime_repr(value)
    if len(value) <= width:
        return value
    split = _split_signature(value)
    if split is None:
        return value
    parameters, suffix = split
    if not parameters:
        return value
    return "(\n" + "".join(f"    {item},\n" for item in parameters) + ")" + suffix


def _kind(value: Any) -> str:
    if inspect.isclass(value):
        return "class"
    if inspect.isroutine(value):
        return "function"
    module = getattr(value, "__module__", None)
    if module in {"collections.abc", "types", "typing"}:
        return "type alias"
    return "constant"


def _summary(value: Any) -> str:
    documentation = inspect.getdoc(value)
    if not documentation:
        return ""
    return " ".join(documentation.splitlines()[0].split())


def _definition(name: str, value: Any, kind: str) -> str:
    if kind in {"class", "function"}:
        try:
            signature = _format_signature(str(inspect.signature(value)))
        except (TypeError, ValueError):
            signature = "(...)"
        return f"{name}{signature}"
    return f"{name} = {_normalize_runtime_repr(repr(value))}"


def _records() -> list[dict[str, str]]:
    names = list(agnoclaw.__all__)
    if len(names) != len(set(names)):
        raise RuntimeError("agnoclaw.__all__ contains duplicate public names")
    records: list[dict[str, str]] = []
    missing_docstrings: list[str] = []
    for name in names:
        if name.startswith("_") or not hasattr(agnoclaw, name):
            raise RuntimeError(f"invalid public export: {name}")
        value = getattr(agnoclaw, name)
        kind = _kind(value)
        summary = _summary(value) if kind in {"class", "function"} else ""
        if kind in {"class", "function"} and not summary:
            missing_docstrings.append(name)
        module = str(getattr(value, "__module__", "agnoclaw"))
        qualname = str(getattr(value, "__qualname__", name))
        records.append(
            {
                "name": name,
                "kind": kind,
                "module": module,
                "qualname": qualname,
                "summary": summary,
                "definition": _definition(name, value, kind),
            }
        )
    if missing_docstrings:
        joined = ", ".join(sorted(missing_docstrings))
        raise RuntimeError(f"public callables missing docstrings: {joined}")
    return records


def _surface_digest(records: list[dict[str, str]]) -> str:
    encoded = json.dumps(
        {"schema_version": SCHEMA_VERSION, "symbols": records},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def generate() -> tuple[str, dict[str, str | int]]:
    records = _records()
    digest = _surface_digest(records)
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for record in records:
        grouped[record["module"]].append(record)

    lines = [
        "# API reference",
        "",
        "Status: generated from the complete top-level `agnoclaw.__all__` contract",
        "",
        "> Do not edit this file by hand. Run",
        "> `uv run python scripts/generate_api_reference.py` and review the diff.",
        "",
        f"Reference schema: `{SCHEMA_VERSION}`  ",
        f"Public symbols: `{len(records)}`  ",
        f"Public-surface digest: `{digest}`",
        "",
        "Every name below is importable directly from `agnoclaw`. Signatures are",
        "generated from the installed runtime objects; source modules identify the",
        "implementation owner, not an additional supported import path.",
        "",
    ]
    for module in sorted(grouped):
        lines.extend((f"## `{module}`", ""))
        for record in sorted(grouped[module], key=lambda item: item["name"].casefold()):
            summary = record["summary"] or "Versioned public value."
            lines.extend(
                (
                    f"### `{record['name']}`",
                    "",
                    f"{record['kind'].title()} · `{record['module']}.{record['qualname']}`",
                    "",
                    summary,
                    "",
                    "```python",
                    f"from agnoclaw import {record['name']}",
                    "",
                    record["definition"],
                    "```",
                    "",
                )
            )
    content = "\n".join(lines).rstrip() + "\n"
    report: dict[str, str | int] = {
        "schema_version": SCHEMA_VERSION,
        "public_symbols": len(records),
        "public_surface_digest": digest,
        "output": str(DEFAULT_OUTPUT.relative_to(ROOT)),
    }
    return content, report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail when the reference is stale.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main() -> int:
    args = _parser().parse_args()
    output = args.output.expanduser().resolve(strict=False)
    content, report = generate()
    report["output"] = str(output.relative_to(ROOT) if output.is_relative_to(ROOT) else output)
    if args.check:
        if not output.is_file() or output.read_text(encoding="utf-8") != content:
            print("generated API reference is stale", file=sys.stderr)
            return 1
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content, encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
