"""Thin CLI entrypoint that degrades cleanly when optional CLI deps are absent."""

from __future__ import annotations

import importlib
import sys
from typing import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    try:
        cli = importlib.import_module("agnoclaw.cli.main").cli
    except ImportError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1

    cli.main(args=list(argv) if argv is not None else None, prog_name="agnoclaw")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
