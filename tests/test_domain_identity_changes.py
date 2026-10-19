"""Different identity kinds and conservative merge/deduplication boundaries."""

from dataclasses import replace

import pytest

from agnoclaw.domain import (
    ChangeSet,
    DeduplicationStatus,
    ObjectId,
    ObjectLink,
    ObjectSnapshot,
    OperationIdentity,
)
from agnoclaw.runtime import HarnessError
from agnoclaw.runtime.security import canonical_json_digest


def snapshot(data, revision="1"):
    return ObjectSnapshot(ObjectId("tenant", "invoice", "inv-1").at(revision), data)


def test_object_identity_and_directed_link_identity_survive_revision_changes():
    invoice = ObjectId("tenant", "invoice", "inv-1")
    first, second = invoice.at("1"), invoice.at("2")
    assert first.identity == second.identity == invoice
    assert first.digest != second.digest
    link = ObjectLink(first, "billed-to", ObjectId("tenant", "customer", "c-1").at("5"))
    newer = replace(link, source=second)
    assert newer.key == link.key
    assert newer.digest != link.digest
    assert replace(link, source=link.target, target=link.source).key != link.key
    with pytest.raises(HarnessError, match="same tenant"):
        replace(link, target=ObjectId("other", "customer", "c-1").at("5"))


def test_idempotency_identity_distinguishes_retry_changed_intent_and_separate_requests():
    first = OperationIdentity(
        "tenant", "pay-invoice", "request-1", canonical_json_digest({"amount": 100})
    )
    assert first.compare(None) is DeduplicationStatus.NEW
    assert first.compare(replace(first)) is DeduplicationStatus.DUPLICATE
    changed = replace(first, intent_digest=canonical_json_digest({"amount": 101}))
    assert changed.key == first.key
    assert changed.compare(first) is DeduplicationStatus.CONFLICT
    with pytest.raises(HarnessError, match="different intent"):
        changed.require_match(first)
    for other in (
        replace(first, tenant_id="other"),
        replace(first, namespace="refund"),
        replace(first, request_id="request-2"),
    ):
        assert other.key != first.key
        with pytest.raises(HarnessError, match="another operation key"):
            other.compare(first)
    # Structured encoding must not alias concatenated identifiers.
    assert (
        replace(first, namespace="a:b", request_id="c").key
        != replace(first, namespace="a", request_id="b:c").key
    )


def test_rebase_merges_disjoint_changes_and_identical_edits_without_mutating_inputs():
    base = snapshot({"amount": 100, "address": {"city": "Paris", "zip": "75001"}, "old": 1})
    proposed = {"amount": 101, "address": {"city": "Paris", "zip": "75001"}}
    changes = ChangeSet(base, proposed)
    proposed["amount"] = 999
    current = snapshot({"amount": 100, "address": {"city": "Lyon", "zip": "75001"}, "old": 1}, "2")
    result = changes.rebase(current)
    assert result.clean
    assert result.require_clean() == {"amount": 101, "address": {"city": "Lyon", "zip": "75001"}}
    assert result.current_target.revision == "2"
    assert base.data["amount"] == current.data["amount"] == 100
    assert changes.rebase(snapshot(proposed | {"amount": 101}, "3")).clean
    with pytest.raises(TypeError):
        result.content["amount"] = 2


def test_conflict_payload_is_renderable_and_cannot_be_silently_accepted():
    base = snapshot({"a/b~c": {"value": 1}})
    result = ChangeSet(base, {"a/b~c": {"value": 2}}).rebase(snapshot({"a/b~c": {"value": 3}}, "2"))
    assert not result.clean
    (conflict,) = result.conflicts
    assert conflict.pointer == "/a~1b~0c/value"
    assert (conflict.base, conflict.current, conflict.proposed) == (1, 3, 2)
    assert result.content["a/b~c"]["value"] == 3
    assert result.to_dict()["conflicts"][0]["path"] == ["a/b~c", "value"]
    with pytest.raises(HarnessError, match="explicit resolution"):
        result.require_clean()


@pytest.mark.parametrize(
    ("base", "current", "proposed"),
    [
        ({"x": 1}, {}, {"x": None}),
        ({"x": False}, {"x": 0}, {"x": True}),
        ({"x": [1, 2]}, {"x": [1, 3]}, {"x": [2, 2]}),
        ({"x": {"a": 1}}, {}, {"x": {"a": 2}}),
        ({}, {"x": 1}, {"x": 2}),
    ],
)
def test_deletions_null_type_changes_arrays_and_overlapping_additions_conflict(
    base, current, proposed
):
    result = ChangeSet(snapshot(base), proposed).rebase(snapshot(current, "2"))
    assert not result.clean
    assert result.to_dict()["content"] == current


def test_null_deletion_and_cross_object_rebase_have_distinct_semantics():
    base = snapshot({"x": None, "y": 1})
    result = ChangeSet(base, {"y": 1}).rebase(snapshot({"x": None, "y": 2}, "2"))
    assert result.clean and result.content == {"y": 2}
    with pytest.raises(HarnessError, match="One object revision"):
        ChangeSet(base, {"y": 1}).rebase(snapshot({"x": None, "y": 2}))
    with pytest.raises(HarnessError, match="another object"):
        ChangeSet(base, {"y": 1}).rebase(
            ObjectSnapshot(ObjectId("other", "invoice", "inv-1").at("2"), {"y": 1})
        )
