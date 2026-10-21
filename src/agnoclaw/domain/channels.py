"""Transport-neutral interactions and explicit channel capability negotiation."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Protocol

from ._contracts import Record, aware, digest_string, fail, frozen_json, identifier
from .components import DomainComponent, timeout
from .identity import ContractRef
from .objects import ObjectRef
from .outputs import OutputSpec


@dataclass(frozen=True)
class Interaction(DomainComponent):
    name: str
    response: OutputSpec
    affordance: str = field(default="question", kw_only=True)
    version: str = field(default="1", kw_only=True)
    kind: ClassVar[str] = "interaction"

    def __post_init__(self) -> None:
        ContractRef(self.name, self.version)
        if self.affordance not in {
            "question",
            "choice",
            "review",
            "comparison",
            "artifact",
            "progress",
        }:
            raise ValueError("unknown interaction affordance")

    def dependencies(self) -> tuple[object, ...]:
        return (self.response,)

    def manifest(self) -> dict[str, Any]:
        return {"affordance": self.affordance, "response": self.response.reference}

    def request(
        self,
        *,
        request_id: str,
        target: ObjectRef,
        subject_digest: str,
        expires_at: datetime,
        payload: Mapping[str, Any],
    ) -> InteractionRequest:
        return InteractionRequest(
            request_id, str(self.ref), self.digest, target, subject_digest, expires_at, payload
        )

    def accept(
        self,
        request: InteractionRequest,
        response: Mapping[str, Any],
        *,
        target: ObjectRef,
        subject_digest: str,
        now: datetime,
    ) -> dict[str, Any]:
        """Validate a response; host authentication and action review remain separate."""
        aware(now)
        if now >= request.expires_at:
            fail("INTERACTION_EXPIRED", "Interaction request has expired.")
        if (
            request.definition_digest != self.digest
            or request.reference != str(self.ref)
            or request.target != target
            or request.subject_digest != subject_digest
        ):
            fail("INTERACTION_CHANGED", "Interaction response concerns stale or different work.")
        return self.response.validate_content(response)

    async def present(
        self,
        adapter: ChannelAdapter,
        request: InteractionRequest,
        *,
        now: datetime,
        timeout_seconds: float = 30,
    ) -> ChannelReceipt:
        """Negotiate before delivery; host adapter owns recipient authentication."""
        aware(now)
        timeout(timeout_seconds)
        adapter.capabilities.require(self)
        if request.reference != str(self.ref) or request.definition_digest != self.digest:
            fail("INTERACTION_CHANGED", "Request belongs to another interaction definition.")
        if now >= request.expires_at:
            fail("INTERACTION_EXPIRED", "Interaction request has expired.")
        async with asyncio.timeout(timeout_seconds):
            receipt = await adapter.present(request)
        if (
            not isinstance(receipt, ChannelReceipt)
            or receipt.request_id != request.request_id
            or receipt.channel != adapter.capabilities.name
        ):
            fail("CHANNEL_RECEIPT_MISMATCH", "Channel returned a receipt for different delivery.")
        return receipt


@dataclass(frozen=True)
class InteractionRequest(Record):
    request_id: str
    reference: str
    definition_digest: str
    target: ObjectRef
    subject_digest: str
    expires_at: datetime
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        identifier(self.request_id, "request_id")
        ContractRef.parse(self.reference)
        digest_string(self.definition_digest)
        digest_string(self.subject_digest)
        aware(self.expires_at)
        object.__setattr__(self, "payload", frozen_json(self.payload))


@dataclass(frozen=True)
class ChannelCapabilities(Record):
    name: str
    protocol_version: str
    affordances: tuple[str, ...]

    def __post_init__(self) -> None:
        identifier(self.name, "name")
        identifier(self.protocol_version, "protocol_version")
        object.__setattr__(self, "affordances", tuple(self.affordances))

    def require(self, interaction: Interaction, *, protocol_version: str = "1") -> None:
        if self.protocol_version != protocol_version:
            fail("CHANNEL_VERSION_MISMATCH", "Channel does not implement the requested protocol.")
        if interaction.affordance not in self.affordances:
            fail(
                "CHANNEL_AFFORDANCE_UNSUPPORTED",
                "Choose an explicit supported presentation adapter.",
            )


@dataclass(frozen=True)
class ChannelReceipt(Record):
    request_id: str
    channel: str
    status: str
    message_id: str | None = None

    def __post_init__(self) -> None:
        identifier(self.request_id, "request_id")
        identifier(self.channel, "channel")
        if self.status not in {"accepted", "delivered", "failed"}:
            raise ValueError("channel status must be accepted, delivered or failed")
        if self.message_id is not None:
            identifier(self.message_id, "message_id")


class ChannelAdapter(Protocol):
    capabilities: ChannelCapabilities

    async def present(self, request: InteractionRequest) -> ChannelReceipt: ...
