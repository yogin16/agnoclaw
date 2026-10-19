"""References to host-owned business objects; no ORM or object database."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from agnoclaw.runtime.context import ExecutionContext

from ._contracts import Record, fail, frozen_json, identifier


@dataclass(frozen=True)
class ObjectId(Record):
    """Stable identity in a host-owned namespace; revision is a separate concern."""

    tenant_id: str
    kind: str
    object_id: str

    def __post_init__(self) -> None:
        for name in ("tenant_id", "kind", "object_id"):
            identifier(getattr(self, name), name)

    def at(self, revision: str) -> ObjectRef:
        return ObjectRef(self.tenant_id, self.kind, self.object_id, revision)


@dataclass(frozen=True)
class ObjectRef(Record):
    tenant_id: str
    kind: str
    object_id: str
    revision: str

    def __post_init__(self) -> None:
        for name in ("tenant_id", "kind", "object_id", "revision"):
            identifier(getattr(self, name), name)

    def require_scope(self, context: ExecutionContext) -> None:
        """Check host-supplied tenant scope; host RBAC still owns object access."""
        if context.tenant_id != self.tenant_id:
            fail("SCOPE_MISMATCH", "Object and execution context tenant scopes differ.")

    @property
    def identity(self) -> ObjectId:
        return ObjectId(self.tenant_id, self.kind, self.object_id)

    def require_current(self, current: ObjectRef) -> None:
        if self != current:
            fail("STALE_OBJECT", "The object identity or revision has changed.")


@dataclass(frozen=True)
class ObjectSnapshot(Record):
    reference: ObjectRef
    data: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", frozen_json(self.data))
