"""Directed, revision-bound relationships between authorized domain objects."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from agnoclaw.runtime.security import canonical_json_digest

from ._contracts import Record, fail, frozen_json, identifier
from .objects import ObjectRef


@dataclass(frozen=True)
class ObjectLink(Record):
    """An asserted relationship, not access permission or proof of claim support.

    ``key`` identifies the directed edge across revisions; ``digest`` identifies
    its exact endpoints and metadata. The host owns cardinality and persistence.
    """

    source: ObjectRef
    relation: str
    target: ObjectRef
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        identifier(self.relation, "relation")
        if self.source.tenant_id != self.target.tenant_id:
            fail("SCOPE_MISMATCH", "Linked objects must belong to the same tenant.")
        object.__setattr__(self, "metadata", frozen_json(self.metadata))

    @property
    def key(self) -> str:
        return canonical_json_digest(
            [
                "domain-link",
                1,
                self.source.identity.to_dict(),
                self.relation,
                self.target.identity.to_dict(),
            ]
        )
