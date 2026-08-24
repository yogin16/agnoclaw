#!/usr/bin/env python3
"""Fail when agnoclaw's provider-neutral package/import surface grows unnoticed."""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
import tomllib
from pathlib import Path

MAX_CORE_DEPENDENCIES = 6
# Final 0.12 rebaseline after the lifecycle, migration, diagnostics, clean-room, and
# support surfaces froze. These ceilings retain about two percent regression headroom;
# the six-dependency and two-second import limits remain unchanged. The oversized
# facade/import graph is explicit post-0.12 extraction debt, not hidden growth.
MAX_SOURCE_BYTES = 3_340_000
MAX_WHEEL_BYTES = 740_000
MAX_SDIST_BYTES = 1_960_000
MAX_SINGLE_MODULE_BYTES = 470_000
MAX_SINGLE_MODULE_LINES = 11_600
MAX_IMPORT_SECONDS = 2.0
MAX_IMPORTED_MODULES = 1_030
FORBIDDEN_CORE_DEPENDENCIES = {
    "anthropic",
    "apscheduler",
    "beautifulsoup4",
    "ddgs",
    "duckduckgo-search",
    "pathspec",
    "python-frontmatter",
}


def _requirement_name(requirement: str) -> str:
    match = re.match(r"^[A-Za-z0-9_.-]+", requirement)
    if match is None:
        raise ValueError(f"cannot parse dependency requirement: {requirement!r}")
    return match.group(0).lower().replace("_", "-")


def _measure_import(iterations: int) -> tuple[float, int]:
    command = [
        sys.executable,
        "-I",
        "-c",
        (
            "import json,sys,time;"
            "started=time.perf_counter();"
            "import agnoclaw;"
            "print(json.dumps({'seconds':time.perf_counter()-started,"
            "'modules':len(sys.modules)}))"
        ),
    ]
    observations: list[dict[str, float | int]] = []
    for _ in range(iterations):
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        observations.append(json.loads(result.stdout))
    return (
        statistics.median(float(item["seconds"]) for item in observations),
        max(int(item["modules"]) for item in observations),
    )


def _latest_artifact(directory: Path, suffix: str) -> Path | None:
    artifacts = sorted(directory.glob(f"agnoclaw-*{suffix}"), key=lambda path: path.stat().st_mtime)
    return artifacts[-1] if artifacts else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--artifacts",
        type=Path,
        help="Directory containing a built wheel and sdist",
    )
    parser.add_argument("--import-iterations", type=int, default=3)
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    with root.joinpath("pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]

    dependencies = list(project["dependencies"])
    dependency_names = {_requirement_name(item) for item in dependencies}
    source_files = tuple(root.joinpath("src", "agnoclaw").rglob("*.py"))
    source_bytes = sum(path.stat().st_size for path in source_files)
    largest_module = max(source_files, key=lambda path: path.stat().st_size)
    largest_module_bytes = largest_module.stat().st_size
    largest_module_lines = largest_module.read_bytes().count(b"\n") + 1
    import_seconds, imported_modules = _measure_import(args.import_iterations)

    metrics: dict[str, object] = {
        "core_dependency_count": len(dependencies),
        "core_dependencies": sorted(dependency_names),
        "source_bytes": source_bytes,
        "largest_module": str(largest_module.relative_to(root)),
        "largest_module_bytes": largest_module_bytes,
        "largest_module_lines": largest_module_lines,
        "median_import_seconds": round(import_seconds, 6),
        "imported_module_count": imported_modules,
    }
    failures: list[str] = []

    if len(dependencies) > MAX_CORE_DEPENDENCIES:
        failures.append(f"core dependencies {len(dependencies)} > budget {MAX_CORE_DEPENDENCIES}")
    forbidden = sorted(dependency_names & FORBIDDEN_CORE_DEPENDENCIES)
    if forbidden:
        failures.append(f"optional/unused dependencies leaked into core: {', '.join(forbidden)}")
    if source_bytes > MAX_SOURCE_BYTES:
        failures.append(f"source bytes {source_bytes} > budget {MAX_SOURCE_BYTES}")
    if largest_module_bytes > MAX_SINGLE_MODULE_BYTES:
        failures.append(
            f"largest module bytes {largest_module_bytes} > budget {MAX_SINGLE_MODULE_BYTES}"
        )
    if largest_module_lines > MAX_SINGLE_MODULE_LINES:
        failures.append(
            f"largest module lines {largest_module_lines} > budget {MAX_SINGLE_MODULE_LINES}"
        )
    if import_seconds > MAX_IMPORT_SECONDS:
        failures.append(f"median import {import_seconds:.3f}s > budget {MAX_IMPORT_SECONDS:.3f}s")
    if imported_modules > MAX_IMPORTED_MODULES:
        failures.append(f"imported modules {imported_modules} > budget {MAX_IMPORTED_MODULES}")

    if args.artifacts is not None:
        artifact_dir = args.artifacts.resolve()
        wheel = _latest_artifact(artifact_dir, ".whl")
        sdist = _latest_artifact(artifact_dir, ".tar.gz")
        if wheel is None or sdist is None:
            failures.append(f"wheel and sdist are required in {artifact_dir}")
        else:
            metrics["wheel_bytes"] = wheel.stat().st_size
            metrics["sdist_bytes"] = sdist.stat().st_size
            if wheel.stat().st_size > MAX_WHEEL_BYTES:
                failures.append(f"wheel bytes {wheel.stat().st_size} > budget {MAX_WHEEL_BYTES}")
            if sdist.stat().st_size > MAX_SDIST_BYTES:
                failures.append(f"sdist bytes {sdist.stat().st_size} > budget {MAX_SDIST_BYTES}")

    metrics["status"] = "failed" if failures else "passed"
    metrics["failures"] = failures
    print(json.dumps(metrics, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
