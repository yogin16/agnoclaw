"""Storage-independent entity/relation definitions and conservative schema evolution."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar

from ._contracts import Record, fail
from .authoring import OutputSchema
from .components import DomainComponent
from .identity import ContractRef
from .links import ObjectLink
from .objects import ObjectSnapshot
from .outputs import OutputSpec
from .verification import CheckResult, CheckStatus, VerificationReport

# One implementation and serialization contract, with a general-purpose authoring name.
Schema = OutputSchema


@dataclass(frozen=True)
class Entity(DomainComponent):
    name: str
    schema: OutputSpec
    version: str = field(default="1", kw_only=True)
    label_fields: tuple[str, ...] = field(default=(), kw_only=True)
    kind: ClassVar[str] = "entity"

    def __post_init__(self) -> None:
        ContractRef(self.name, self.version)
        if not isinstance(self.schema, OutputSpec):
            raise TypeError("entity schema must be a Schema or OutputSpec")
        object.__setattr__(self, "label_fields", tuple(self.label_fields))
        fields = self.schema.json_schema().get("properties", {})
        if len(set(self.label_fields)) != len(self.label_fields) or any(
            name not in fields for name in self.label_fields
        ):
            raise ValueError("label_fields must name unique schema properties")

    def dependencies(self) -> tuple[object, ...]:
        return (self.schema,)

    def manifest(self) -> dict[str, Any]:
        return {"schema": self.schema.reference, "label_fields": list(self.label_fields)}

    def validate(self, snapshot: ObjectSnapshot) -> dict[str, Any]:
        if snapshot.reference.kind != self.name:
            fail("ENTITY_KIND_MISMATCH", "Object kind does not match the entity definition.")
        return self.schema.validate_content(snapshot.data)


@dataclass(frozen=True)
class Relation(DomainComponent):
    name: str
    source: Entity
    target: Entity
    version: str = field(default="1", kw_only=True)
    max_targets: int | None = field(default=None, kw_only=True)
    max_sources: int | None = field(default=None, kw_only=True)
    edge_schema: OutputSpec | None = field(default=None, kw_only=True)
    kind: ClassVar[str] = "relation"

    def __post_init__(self) -> None:
        ContractRef(self.name, self.version)
        for limit in (self.max_targets, self.max_sources):
            if limit is not None and (type(limit) is not int or limit < 1):
                raise ValueError("relation limits must be positive integers or None")

    def dependencies(self) -> tuple[object, ...]:
        return (self.source, self.target) + ((self.edge_schema,) if self.edge_schema else ())

    def manifest(self) -> dict[str, Any]:
        return {
            "source": str(self.source.ref),
            "target": str(self.target.ref),
            "max_targets": self.max_targets,
            "max_sources": self.max_sources,
            "edge_schema": self.edge_schema.reference if self.edge_schema else None,
        }

    def link(self, source: ObjectSnapshot, target: ObjectSnapshot, **metadata: Any) -> ObjectLink:
        self.source.validate(source)
        self.target.validate(target)
        if self.edge_schema:
            metadata = self.edge_schema.validate_content(metadata)
        return ObjectLink(source.reference, self.name, target.reference, metadata)

    def verify(
        self,
        link: ObjectLink,
        *,
        neighbors: Sequence[ObjectLink] = (),
        complete: bool = False,
    ) -> VerificationReport:
        """Validate a host-supplied view; incomplete cardinality is UNKNOWN.

        complete=True is the host's assertion about its authorized snapshot. This
        report is not a transaction lock or proof that referenced objects exist.
        """
        shape = (
            link.relation == self.name
            and link.source.kind == self.source.name
            and link.target.kind == self.target.name
        )
        try:
            if self.edge_schema:
                self.edge_schema.validate_content(link.metadata)
        except (ValueError, TypeError):
            shape = False
        edges = {link.key: link}
        for item in neighbors:
            if item.source.tenant_id != link.source.tenant_id:
                fail("SCOPE_MISMATCH", "Relation view contains another tenant.")
            if item.relation == self.name:
                edges[item.key] = item
        sources = sum(e.target.identity == link.target.identity for e in edges.values())
        targets = sum(e.source.identity == link.source.identity for e in edges.values())
        violated = (self.max_sources is not None and sources > self.max_sources) or (
            self.max_targets is not None and targets > self.max_targets
        )
        constrained = self.max_sources is not None or self.max_targets is not None
        cardinality = (
            CheckStatus.FAIL
            if violated
            else CheckStatus.UNKNOWN
            if constrained and not complete
            else CheckStatus.PASS
        )
        return VerificationReport(
            link.digest,
            (
                CheckResult(
                    f"{self.name}.shape",
                    self.version,
                    link.digest,
                    CheckStatus.PASS if shape else CheckStatus.FAIL,
                    "Relation shape checked.",
                ),
                CheckResult(
                    f"{self.name}.cardinality",
                    self.version,
                    link.digest,
                    cardinality,
                    "Cardinality uses the supplied host view.",
                ),
            ),
        )


@dataclass(frozen=True)
class SchemaCompatibility(Record):
    old_digest: str
    new_digest: str
    direction: str
    status: str
    reasons: tuple[str, ...]


def compare_schemas(
    old: OutputSpec, new: OutputSpec, *, direction: str = "read_old"
) -> SchemaCompatibility:
    """Conservative shape comparison, never certification of Python validators.

    read_old: new consumes old data. write_old: old consumes new data.
    Changed shapes outside the small supported subset return unknown.
    """
    if direction not in {"read_old", "write_old"}:
        raise ValueError("direction must be read_old or write_old")
    source, consumer = (old, new) if direction == "read_old" else (new, old)
    a, b = source.json_schema(), consumer.json_schema()
    reasons: list[str] = []
    status = "unknown"
    required = set(b.get("required", ())) - set(a.get("required", ()))
    if required:
        reasons.append("Consumer requires additional fields: " + ", ".join(sorted(required)))
        status = "incompatible"
    removed = set(a.get("properties", {})) - set(b.get("properties", {}))
    if removed and b.get("additionalProperties") is False:
        reasons.append(
            "Consumer rejects fields the producer may supply: " + ", ".join(sorted(removed))
        )
        status = "incompatible"
    if not reasons:
        reasons.append("Python validators and semantic constraints require domain fixtures.")
        if old is new:
            status = "identical"
        elif a != b:
            reasons.append("Schema shape changed; inspect the definition diff.")
    return SchemaCompatibility(old.digest, new.digest, direction, status, tuple(reasons))
