"""Explicit contract and operation identities; fingerprints are not business IDs."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from agnoclaw.runtime.security import canonical_json_digest

from ._contracts import Record, digest_string, fail, identifier


@dataclass(frozen=True)
class ContractRef(Record):
    """A name and an explicit compatibility version, rendered as ``name@version``."""

    name: str
    version: str = "1"

    def __post_init__(self) -> None:
        for field in ("name", "version"):
            value = getattr(self, field)
            identifier(value, field)
            if "@" in value or any(char.isspace() for char in value):
                raise ValueError(f"{field} cannot contain '@' or whitespace")

    def __str__(self) -> str:
        return f"{self.name}@{self.version}"

    @classmethod
    def parse(cls, value: str) -> ContractRef:
        if not isinstance(value, str) or value.count("@") != 1:
            raise ValueError("expected an explicit name@version reference")
        return cls(*value.split("@"))


class DeduplicationStatus(StrEnum):
    NEW = "new"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class OperationIdentity(Record):
    """One host-issued request and its immutable intent, separate from run attempts.

    Persist/claim the key atomically in the host store. Comparing records in memory
    does not reserve a key, settle an effect, or replace runtime reconciliation.
    """

    tenant_id: str
    namespace: str
    request_id: str
    intent_digest: str

    def __post_init__(self) -> None:
        for field in ("tenant_id", "namespace", "request_id"):
            identifier(getattr(self, field), field)
        digest_string(self.intent_digest)

    @property
    def key(self) -> str:
        # Do not include the payload: changed content under one request must conflict.
        return canonical_json_digest(
            ["domain-operation", 1, self.tenant_id, self.namespace, self.request_id]
        )

    def compare(self, existing: OperationIdentity | None) -> DeduplicationStatus:
        if existing is None:
            return DeduplicationStatus.NEW
        if existing.key != self.key:
            fail("DEDUPE_SCOPE_MISMATCH", "The receipt belongs to another operation key.")
        return (
            DeduplicationStatus.DUPLICATE
            if existing.intent_digest == self.intent_digest
            else DeduplicationStatus.CONFLICT
        )

    def require_match(self, existing: OperationIdentity) -> None:
        if self.compare(existing) is DeduplicationStatus.CONFLICT:
            fail("IDEMPOTENCY_CONFLICT", "This request ID already identifies different intent.")
