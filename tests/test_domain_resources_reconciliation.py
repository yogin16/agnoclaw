"""Pinned workspace bytes and semantic reconciliation retain scope and provenance."""

from dataclasses import replace

import pytest

from agnoclaw.domain import (
    Domain,
    FieldClaim,
    LocalResources,
    MatchPolicy,
    ObjectSnapshot,
    ResolutionPolicy,
    Resources,
    WorkspaceSnapshot,
    locate_quote,
)
from agnoclaw.runtime import HarnessError
from tests.test_domain_core import source, target


def test_workspace_capture_overlay_and_materialization_require_exact_content(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "SKILL.md").write_text("Original instructions")
    resources = Resources("skills", ("SKILL.md",))
    compiled = Domain("pack", components=(resources,)).compile()
    snapshot = compiled.use(resources).capture(LocalResources(root), tenant_id="tenant-1")
    original = snapshot.digest
    (root / "SKILL.md").write_text("Changed instructions")
    assert snapshot.digest == original
    assert resources.capture(LocalResources(root), tenant_id="tenant-1").digest != original
    with pytest.raises(HarnessError, match="exact hashes"):
        snapshot.overlay({"SKILL.md": b"override"}, expected={})
    overlay = snapshot.overlay({"SKILL.md": b"override"}, expected=snapshot.manifest()["files"])
    assert overlay.base_digest == original
    destination = tmp_path / "pinned"
    overlay.materialize(destination, tenant_id="tenant-1", expected_digest=overlay.digest)
    assert (destination / "SKILL.md").read_bytes() == b"override"
    with pytest.raises(FileExistsError):
        overlay.materialize(destination, tenant_id="tenant-1", expected_digest=overlay.digest)
    with pytest.raises(HarnessError, match="tenant"):
        overlay.materialize(tmp_path / "foreign", tenant_id="other", expected_digest=overlay.digest)
    with pytest.raises(HarnessError, match="pinned content"):
        overlay.materialize(tmp_path / "stale", tenant_id="tenant-1", expected_digest=original)
    assert not list(tmp_path.glob(".domain-stage-*"))


def test_workspace_provider_bounds_and_path_containment(tmp_path):
    (tmp_path / "large").write_bytes(b"12345")
    with pytest.raises(ValueError, match="max_bytes"):
        Resources("r", ("large",), max_bytes=4).capture(LocalResources(tmp_path), tenant_id="t")
    (tmp_path / "link").symlink_to(tmp_path / "large")
    with pytest.raises(ValueError, match="symbolic"):
        Resources("r", ("link",)).capture(LocalResources(tmp_path), tenant_id="t")
    for path in ("../escape", "/absolute", "a/../b", "a//b", "a\\b"):
        with pytest.raises(ValueError, match="relative"):
            Resources("r", (path,))
    with pytest.raises(ValueError, match="both a file and directory"):
        WorkspaceSnapshot("t", "sha256:" + "0" * 64, {"a": b"", "a/b": b""})


def test_matching_ambiguity_errors_and_scope_do_not_merge_records():
    subject = ObjectSnapshot(target(), {"name": "Acme"})
    a = replace(subject, reference=replace(target(), object_id="a"))
    b = replace(subject, reference=replace(target(), object_id="b"))
    policy = MatchPolicy("company-id", lambda x, y: 1.0, implementation_id="matcher-v1")
    assert policy.match(subject, (a,)).status == "match"
    assert policy.match(subject, (a, b)).status == "review"
    assert policy.match(subject, ()).status == "no_match"
    assert replace(policy, compare=lambda x, y: float("nan")).match(subject, (a,)).status == "error"
    with pytest.raises(ValueError, match="one revision"):
        policy.match(subject, (a, replace(a, reference=replace(a.reference, revision="2"))))
    with pytest.raises(HarnessError, match="tenant and kind"):
        policy.match(subject, (replace(a, reference=replace(a.reference, tenant_id="other")),))


def test_claim_resolution_keeps_conflicts_qualifiers_and_source_evidence():
    evidence = locate_quote(source(), "Revenue: USD 120").evidence
    assert evidence is not None
    claim = FieldClaim(
        target().identity,
        "revenue",
        120,
        "filing",
        (evidence,),
        {"period": "2025", "currency": "USD"},
    )
    other = replace(claim, value=125, source="deck")
    policy = ResolutionPolicy("financial-source-priority", ("filing", "deck"))
    result = policy.resolve((claim, other, claim))
    assert result.status == "preferred" and result.preferred == claim
    assert result.claims == (claim, other)
    assert policy.resolve((claim, replace(claim, value=130))).status == "conflict"
    assert (
        policy.resolve((claim, replace(other, qualifiers={"period": "2026"}))).status
        == "incomparable"
    )
    assert policy.resolve((replace(claim, source="unknown"),)).status == "unresolved"
    with pytest.raises(HarnessError, match="one object field"):
        policy.resolve((claim, replace(other, target=replace(claim.target, object_id="elsewhere"))))
