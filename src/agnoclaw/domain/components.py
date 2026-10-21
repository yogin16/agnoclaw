"""Small declaration protocol shared by opt-in domain components."""

from __future__ import annotations

import inspect
import math
from collections.abc import Awaitable, Mapping
from typing import Any, ClassVar, TypeVar

from agnoclaw.runtime.security import canonical_json_digest

from .identity import ContractRef

T = TypeVar("T")


async def resolve(value: T | Awaitable[T]) -> T:
    return await value if inspect.isawaitable(value) else value


def timeout(value: float) -> None:
    if not math.isfinite(value) or value <= 0:
        raise ValueError("timeout must be positive and finite")


class DomainComponent:
    """Trusted Python definition. Manifests contain data, never executable imports.

    Extensions implement manifest() and dependencies(). The catalog does not invoke
    their business callbacks. Change versions/build provenance when behavior changes.
    """

    kind: ClassVar[str]
    name: str
    version: str

    @property
    def ref(self) -> ContractRef:
        return ContractRef(self.name, self.version)

    def manifest(self) -> Mapping[str, Any]:
        raise NotImplementedError

    def dependencies(self) -> tuple[object, ...]:
        return ()

    @property
    def digest(self) -> str:
        return canonical_json_digest(
            [
                self.kind,
                str(self.ref),
                self.manifest(),
                [component_digest(item) for item in self.dependencies()],
            ]
        )


def component_digest(value: object) -> str:
    from .decisions import DecisionTable

    if isinstance(value, DecisionTable):
        return canonical_json_digest(
            [
                value.name,
                value.version,
                value.implementation_digest,
                [(rule.rule_id, rule.outcome) for rule in value.rules],
            ]
        )
    digest = getattr(value, "digest", None)
    if not isinstance(digest, str):
        raise TypeError(f"{type(value).__name__} is not a fingerprinted domain definition")
    return digest
