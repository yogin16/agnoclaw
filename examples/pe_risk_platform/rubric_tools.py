"""Utilities for runtime rubric tuning in interactive workflows."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_rubric(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save_rubric(path: Path, rubric: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rubric, indent=2), encoding="utf-8")


def set_weight_renormalized(
    rubric: dict[str, Any],
    dimension: str,
    new_weight: float,
) -> dict[str, Any]:
    if not 0.0 <= new_weight <= 1.0:
        raise ValueError("new_weight must be between 0 and 1")

    weights = dict(rubric.get("weights") or {})
    if dimension not in weights:
        raise KeyError(f"Unknown dimension: {dimension}")

    other_keys = [k for k in weights if k != dimension]
    other_sum = sum(float(weights[k]) for k in other_keys)
    remaining = max(0.0, 1.0 - new_weight)

    if other_keys:
        if other_sum > 0:
            scale = remaining / other_sum
            for key in other_keys:
                weights[key] = float(weights[key]) * scale
        else:
            even = remaining / len(other_keys)
            for key in other_keys:
                weights[key] = even

    weights[dimension] = new_weight

    # Numerical cleanup to keep exact 1.0 total.
    total = sum(float(v) for v in weights.values())
    if total != 0:
        adjust = 1.0 - total
        # Push remainder into the tuned dimension for determinism.
        weights[dimension] = float(weights[dimension]) + adjust

    rubric_out = dict(rubric)
    rubric_out["weights"] = weights
    return rubric_out
