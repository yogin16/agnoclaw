"""Release-pipeline security invariants."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_ACTION_RE = re.compile(r"^\s*(?:-\s*)?uses:\s*([^@\s]+)@([^\s#]+)", re.MULTILINE)
_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")


def _workflow(path: str) -> tuple[Path, str, dict]:
    workflow_path = Path(path)
    text = workflow_path.read_text(encoding="utf-8")
    return workflow_path, text, yaml.safe_load(text)


def test_every_external_action_is_pinned_to_a_full_commit_sha() -> None:
    for workflow_path in sorted(Path(".github/workflows").glob("*.yml")):
        text = workflow_path.read_text(encoding="utf-8")
        actions = _ACTION_RE.findall(text)
        assert actions, f"expected action dependencies in {workflow_path}"
        mutable = [f"{owner}@{ref}" for owner, ref in actions if not _FULL_SHA_RE.fullmatch(ref)]
        assert mutable == [], f"mutable action references in {workflow_path}: {mutable}"


def test_release_authority_is_split_between_publish_and_tag_jobs() -> None:
    _, _, workflow = _workflow(".github/workflows/publish.yml")

    assert workflow["permissions"] == {"contents": "read"}
    assert "permissions" not in workflow["jobs"]["test"]
    assert workflow["jobs"]["publish"]["permissions"] == {
        "contents": "read",
        "id-token": "write",
    }
    assert workflow["jobs"]["tag"]["permissions"] == {"contents": "write"}
    assert "id-token" not in workflow["jobs"]["tag"]["permissions"]


def test_ci_tool_bootstrap_and_workflow_ownership_are_pinned() -> None:
    workflow_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(Path(".github/workflows").glob("*.yml"))
    )
    assert "pip install --upgrade uv" not in workflow_text
    assert re.search(r'\n\s+version: "0\.9\.15"\s*$', workflow_text, re.MULTILINE)

    owners = Path(".github/CODEOWNERS").read_text(encoding="utf-8")
    assert "/.github/workflows/ @yogin16" in owners
    assert "/.github/CODEOWNERS @yogin16" in owners


def test_locked_dependency_audit_covers_every_optional_dependency_set() -> None:
    _, _, workflow = _workflow(".github/workflows/ci.yml")

    audit = workflow["jobs"]["dependency-audit"]
    commands = "\n".join(step.get("run", "") for step in audit["steps"])
    assert "uv export --all-extras --frozen" in commands
    assert "pip-audit --strict --no-deps --disable-pip" in commands
    assert "uv run --no-sync pip-audit" in commands
    assert 'pip-audit==2.10.1' in Path("pyproject.toml").read_text(encoding="utf-8")
