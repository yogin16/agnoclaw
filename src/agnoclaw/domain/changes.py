"""Explicit three-way JSON change reconciliation; commits remain host-owned."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from agnoclaw.runtime.security import canonical_json_digest, thaw_data

from ._contracts import Record, fail, frozen_json
from .objects import ObjectRef, ObjectSnapshot

_MISSING = object()


def _equal(left: Any, right: Any) -> bool:
    if left is _MISSING or right is _MISSING:
        return left is right
    # Python treats True == 1 and 1 == 1.0; JSON type changes must stay observable.
    return canonical_json_digest(left) == canonical_json_digest(right)


@dataclass(frozen=True)
class ChangeConflict(Record):
    path: tuple[str, ...]
    base_present: bool
    base: Any
    current_present: bool
    current: Any
    proposed_present: bool
    proposed: Any

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", tuple(self.path))
        for name in ("base", "current", "proposed"):
            object.__setattr__(self, name, frozen_json(getattr(self, name)))

    @property
    def pointer(self) -> str:
        return "".join("/" + part.replace("~", "~0").replace("/", "~1") for part in self.path)


@dataclass(frozen=True)
class ChangeResolution(Record):
    current_target: ObjectRef
    content: Mapping[str, Any]
    conflicts: tuple[ChangeConflict, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.content, Mapping):
            raise TypeError("resolved content must be a JSON object")
        object.__setattr__(self, "content", frozen_json(self.content))
        object.__setattr__(self, "conflicts", tuple(self.conflicts))

    @property
    def clean(self) -> bool:
        return not self.conflicts

    def require_clean(self) -> Mapping[str, Any]:
        if self.conflicts:
            fail("CHANGE_CONFLICT", "Concurrent edits need explicit resolution and fresh review.")
        return self.content


@dataclass(frozen=True)
class ChangeSet(Record):
    """A proposed complete JSON document relative to one exact base snapshot.

    Objects merge recursively; arrays/scalars are atomic. Missing differs from null.
    Overlapping edits keep the current value in the preview and expose a conflict.
    Every rebase needs fresh validation/review and an atomic expected-revision write.
    """

    base: ObjectSnapshot
    proposed: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.proposed, Mapping) or not isinstance(self.base.data, Mapping):
            raise TypeError("change sets require JSON objects")
        object.__setattr__(self, "proposed", frozen_json(self.proposed))

    def rebase(self, current: ObjectSnapshot) -> ChangeResolution:
        if not isinstance(current.data, Mapping):
            raise TypeError("current snapshot must contain a JSON object")
        if self.base.reference.identity != current.reference.identity:
            fail("OBJECT_MISMATCH", "Changes cannot be rebased onto another object or tenant.")
        if self.base.reference == current.reference and not _equal(self.base.data, current.data):
            fail("REVISION_CONFLICT", "One object revision describes different content.")
        conflicts: list[ChangeConflict] = []

        def merge(base: Any, present: Any, proposed: Any, path: tuple[str, ...]) -> Any:
            if _equal(proposed, base) or _equal(present, proposed):
                return present
            if _equal(present, base):
                return proposed
            if all(isinstance(value, Mapping) for value in (base, present, proposed)):
                result = {}
                for key in sorted(set(base) | set(present) | set(proposed)):
                    value = merge(
                        base.get(key, _MISSING),
                        present.get(key, _MISSING),
                        proposed.get(key, _MISSING),
                        (*path, key),
                    )
                    if value is not _MISSING:
                        result[key] = value
                return result
            conflicts.append(
                ChangeConflict(
                    path,
                    base is not _MISSING,
                    None if base is _MISSING else base,
                    present is not _MISSING,
                    None if present is _MISSING else present,
                    proposed is not _MISSING,
                    None if proposed is _MISSING else proposed,
                )
            )
            return present

        content = merge(
            thaw_data(self.base.data), thaw_data(current.data), thaw_data(self.proposed), ()
        )
        return ChangeResolution(current.reference, content, tuple(conflicts))
