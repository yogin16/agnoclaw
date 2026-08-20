"""Immutable learning intent and run-resolved scope contracts.

The public policy contains no caller identity.  A :class:`LearningScope` is resolved
from the trusted execution context immediately before a run and produces opaque Agno
storage keys.  This keeps reusable harness configuration separate from tenant, user,
and session authority.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from .runtime.errors import HarnessError

if TYPE_CHECKING:
    from .runtime.context import ExecutionContext


class LearningMode(StrEnum):
    """Agno learning modes exposed without leaking Agno enum types."""

    ALWAYS = "always"
    AGENTIC = "agentic"
    PROPOSE = "propose"
    HITL = "hitl"


class LearningWritePath(StrEnum):
    """Where a store's proposed writes must go."""

    DIRECT = "direct"
    CANDIDATE = "candidate"


class LearningPromotion(StrEnum):
    """Promotion authority for evaluated learning candidates."""

    DISABLED = "disabled"
    REVIEWED = "reviewed"
    AUTOMATIC_EXPERIMENTAL = "automatic_experimental"


@dataclass(frozen=True, slots=True)
class LearningStorePolicy:
    """Policy for one Agno Learning Store."""

    mode: LearningMode
    write_path: LearningWritePath = LearningWritePath.DIRECT
    max_updates_per_run: int = 5

    def __post_init__(self) -> None:
        if not isinstance(self.mode, LearningMode):
            try:
                object.__setattr__(self, "mode", LearningMode(self.mode))
            except (TypeError, ValueError) as exc:
                raise HarnessError(
                    code="LEARNING_MODE_UNSUPPORTED",
                    category="learning",
                    message=f"Unsupported learning mode {self.mode!r}.",
                    retryable=False,
                    details={
                        "mode": str(self.mode),
                        "supported_modes": [mode.value for mode in LearningMode],
                    },
                ) from exc
        if not isinstance(self.write_path, LearningWritePath):
            try:
                object.__setattr__(
                    self,
                    "write_path",
                    LearningWritePath(self.write_path),
                )
            except (TypeError, ValueError) as exc:
                raise HarnessError(
                    code="LEARNING_WRITE_PATH_UNSUPPORTED",
                    category="learning",
                    message=f"Unsupported learning write path {self.write_path!r}.",
                    retryable=False,
                    details={"write_path": str(self.write_path)},
                ) from exc
        if not 1 <= self.max_updates_per_run <= 100:
            raise HarnessError(
                code="LEARNING_BUDGET_INVALID",
                category="learning",
                message="max_updates_per_run must be between 1 and 100.",
                retryable=False,
                details={"max_updates_per_run": self.max_updates_per_run},
            )

    def descriptor(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "write_path": self.write_path.value,
            "max_updates_per_run": self.max_updates_per_run,
        }


def _store_policy(
    value: str | LearningMode | LearningStorePolicy | None,
    *,
    write_path: LearningWritePath,
    max_updates_per_run: int,
) -> LearningStorePolicy | None:
    if value is None:
        return None
    if isinstance(value, LearningStorePolicy):
        return value
    try:
        mode = value if isinstance(value, LearningMode) else LearningMode(value)
    except (TypeError, ValueError) as exc:
        raise HarnessError(
            code="LEARNING_MODE_UNSUPPORTED",
            category="learning",
            message=f"Unsupported learning mode {value!r}.",
            retryable=False,
            details={
                "mode": str(value),
                "supported_modes": [mode.value for mode in LearningMode],
            },
        ) from exc
    return LearningStorePolicy(
        mode=mode,
        write_path=write_path,
        max_updates_per_run=max_updates_per_run,
    )


@dataclass(frozen=True, slots=True)
class LearningPolicy:
    """Static learning intent compiled once with a reusable harness.

    Personal and Session Context writes may be direct. Institutional stores are
    candidate-only: a model cannot promote its own reflection into trusted shared
    memory. ``knowledge`` is a host-managed Agno resource and is deliberately omitted
    from equality, repr, and the serializable descriptor.
    """

    user_profile: LearningStorePolicy | None = None
    user_memory: LearningStorePolicy | None = None
    session_context: LearningStorePolicy | None = None
    entity_memory: LearningStorePolicy | None = None
    learned_knowledge: LearningStorePolicy | None = None
    decision_log: LearningStorePolicy | None = None
    namespace: str | None = None
    promotion: LearningPromotion = LearningPromotion.REVIEWED
    tenant_required: bool = False
    consent_required: bool = False
    retention_days: int | None = None
    knowledge: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        for store_name in (
            "user_profile",
            "user_memory",
            "session_context",
            "entity_memory",
            "learned_knowledge",
            "decision_log",
        ):
            store = getattr(self, store_name)
            if store is not None and not isinstance(store, LearningStorePolicy):
                raise HarnessError(
                    code="LEARNING_STORE_POLICY_INVALID",
                    category="learning",
                    message=f"{store_name} must be a LearningStorePolicy or None.",
                    retryable=False,
                    details={"store": store_name},
                )
        if not isinstance(self.promotion, LearningPromotion):
            try:
                object.__setattr__(
                    self,
                    "promotion",
                    LearningPromotion(self.promotion),
                )
            except (TypeError, ValueError) as exc:
                raise HarnessError(
                    code="LEARNING_PROMOTION_UNSUPPORTED",
                    category="learning",
                    message=f"Unsupported learning promotion policy {self.promotion!r}.",
                    retryable=False,
                    details={"promotion": str(self.promotion)},
                ) from exc
        if self.retention_days is not None and not 1 <= self.retention_days <= 36_500:
            raise HarnessError(
                code="LEARNING_RETENTION_INVALID",
                category="learning",
                message="retention_days must be between 1 and 36500.",
                retryable=False,
                details={"retention_days": self.retention_days},
            )

        institutional = self.institutional_stores
        if institutional and (not isinstance(self.namespace, str) or not self.namespace.strip()):
            raise HarnessError(
                code="LEARNING_NAMESPACE_REQUIRED",
                category="learning",
                message="Institutional learning requires an explicit namespace.",
                retryable=False,
                details={"stores": list(institutional)},
            )
        if self.learned_knowledge is not None and (
            self.knowledge is None or getattr(self.knowledge, "vector_db", None) is None
        ):
            raise HarnessError(
                code="LEARNING_KNOWLEDGE_REQUIRED",
                category="learning",
                message=("Learned Knowledge requires Agno Knowledge with a configured vector_db."),
                retryable=False,
                details={"store": "learned_knowledge"},
            )
        for store_name in institutional:
            store = getattr(self, store_name)
            if store.write_path is not LearningWritePath.CANDIDATE:
                raise HarnessError(
                    code="LEARNING_DIRECT_INSTITUTIONAL_WRITE_FORBIDDEN",
                    category="learning",
                    message=(
                        f"{store_name} must use the candidate write path; shared "
                        "learning cannot be promoted by the proposing agent."
                    ),
                    retryable=False,
                    details={"store": store_name},
                )

    @property
    def personal_stores(self) -> tuple[str, ...]:
        return tuple(
            name for name in ("user_profile", "user_memory") if getattr(self, name) is not None
        )

    @property
    def institutional_stores(self) -> tuple[str, ...]:
        return tuple(
            name
            for name in ("entity_memory", "learned_knowledge", "decision_log")
            if getattr(self, name) is not None
        )

    @property
    def enabled(self) -> bool:
        return bool(
            self.personal_stores or self.session_context is not None or self.institutional_stores
        )

    def descriptor(self) -> dict[str, Any]:
        stores = {}
        for name in (
            "user_profile",
            "user_memory",
            "session_context",
            "entity_memory",
            "learned_knowledge",
            "decision_log",
        ):
            store = getattr(self, name)
            stores[name] = store.descriptor() if store is not None else None
        return {
            "schema_version": 1,
            "stores": stores,
            "namespace": self.namespace,
            "promotion": self.promotion.value,
            "tenant_required": self.tenant_required,
            "consent_required": self.consent_required,
            "retention_days": self.retention_days,
            "knowledge_configured": self.knowledge is not None,
        }


class LearningProfile:
    """Small named constructors for common :class:`LearningPolicy` shapes."""

    @staticmethod
    def disabled() -> LearningPolicy:
        return LearningPolicy(promotion=LearningPromotion.DISABLED)

    @staticmethod
    def personal(
        *,
        user_profile: str | LearningMode | LearningStorePolicy | None = "always",
        user_memory: str | LearningMode | LearningStorePolicy | None = "agentic",
        max_updates_per_run: int = 5,
        tenant_required: bool = False,
        consent_required: bool = True,
        retention_days: int | None = None,
    ) -> LearningPolicy:
        return LearningPolicy(
            user_profile=_store_policy(
                user_profile,
                write_path=LearningWritePath.DIRECT,
                max_updates_per_run=max_updates_per_run,
            ),
            user_memory=_store_policy(
                user_memory,
                write_path=LearningWritePath.DIRECT,
                max_updates_per_run=max_updates_per_run,
            ),
            tenant_required=tenant_required,
            consent_required=consent_required,
            retention_days=retention_days,
        )

    @staticmethod
    def session(
        *,
        session_context: str | LearningMode | LearningStorePolicy = "always",
        max_updates_per_run: int = 5,
        tenant_required: bool = False,
        retention_days: int | None = None,
    ) -> LearningPolicy:
        return LearningPolicy(
            session_context=_store_policy(
                session_context,
                write_path=LearningWritePath.DIRECT,
                max_updates_per_run=max_updates_per_run,
            ),
            tenant_required=tenant_required,
            retention_days=retention_days,
        )

    @staticmethod
    def personal_and_session(
        *,
        user_profile: str | LearningMode | LearningStorePolicy | None = "always",
        user_memory: str | LearningMode | LearningStorePolicy | None = "agentic",
        session_context: str | LearningMode | LearningStorePolicy | None = "always",
        max_updates_per_run: int = 5,
        tenant_required: bool = False,
        consent_required: bool = True,
        retention_days: int | None = None,
    ) -> LearningPolicy:
        personal = LearningProfile.personal(
            user_profile=user_profile,
            user_memory=user_memory,
            max_updates_per_run=max_updates_per_run,
            tenant_required=tenant_required,
            consent_required=consent_required,
            retention_days=retention_days,
        )
        return LearningPolicy(
            user_profile=personal.user_profile,
            user_memory=personal.user_memory,
            session_context=_store_policy(
                session_context,
                write_path=LearningWritePath.DIRECT,
                max_updates_per_run=max_updates_per_run,
            ),
            tenant_required=tenant_required,
            consent_required=consent_required,
            retention_days=retention_days,
        )

    @staticmethod
    def institutional(
        *,
        namespace: str,
        knowledge: Any = None,
        entity_memory: str | LearningMode | LearningStorePolicy | None = "agentic",
        learned_knowledge: str | LearningMode | LearningStorePolicy | None = "agentic",
        decision_log: str | LearningMode | LearningStorePolicy | None = "agentic",
        max_updates_per_run: int = 5,
        promotion: str | LearningPromotion = LearningPromotion.REVIEWED,
        retention_days: int | None = None,
    ) -> LearningPolicy:
        return LearningPolicy(
            entity_memory=_store_policy(
                entity_memory,
                write_path=LearningWritePath.CANDIDATE,
                max_updates_per_run=max_updates_per_run,
            ),
            learned_knowledge=_store_policy(
                learned_knowledge,
                write_path=LearningWritePath.CANDIDATE,
                max_updates_per_run=max_updates_per_run,
            ),
            decision_log=_store_policy(
                decision_log,
                write_path=LearningWritePath.CANDIDATE,
                max_updates_per_run=max_updates_per_run,
            ),
            namespace=namespace,
            promotion=promotion,  # type: ignore[arg-type]
            tenant_required=True,
            retention_days=retention_days,
            knowledge=knowledge,
        )


def _storage_key(kind: str, values: dict[str, str | None]) -> str:
    canonical = json.dumps(values, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"agnoclaw:{kind}:v1:{digest}"


@dataclass(frozen=True, slots=True)
class LearningScope:
    """Trusted run identity mapped to collision-resistant Agno storage keys."""

    tenant_id: str | None
    org_id: str | None
    agent_id: str
    user_id: str | None
    session_id: str | None
    namespace: str | None
    storage_namespace: str
    storage_user_id: str | None
    storage_session_id: str | None
    retention_days: int | None
    consented: bool

    @classmethod
    def resolve(
        cls,
        policy: LearningPolicy,
        context: ExecutionContext,
        *,
        agent_id: str,
        consented: bool = False,
    ) -> LearningScope:
        if not policy.enabled:
            return cls(
                tenant_id=context.tenant_id,
                org_id=context.org_id,
                agent_id=agent_id,
                user_id=context.user_id,
                session_id=context.session_id,
                namespace=policy.namespace,
                storage_namespace=_storage_key(
                    "namespace",
                    {"tenant_id": context.tenant_id, "agent_id": agent_id, "namespace": None},
                ),
                storage_user_id=None,
                storage_session_id=None,
                retention_days=policy.retention_days,
                consented=consented,
            )
        if policy.tenant_required and not context.tenant_id:
            raise HarnessError(
                code="LEARNING_SCOPE_TENANT_REQUIRED",
                category="learning",
                message="This learning policy requires a trusted tenant_id.",
                retryable=False,
                details={"field": "tenant_id"},
            )
        if policy.personal_stores and not context.user_id:
            raise HarnessError(
                code="LEARNING_SCOPE_USER_REQUIRED",
                category="learning",
                message="Personal learning requires a trusted user_id.",
                retryable=False,
                details={"field": "user_id", "stores": list(policy.personal_stores)},
            )
        if policy.session_context is not None and not context.session_id:
            raise HarnessError(
                code="LEARNING_SCOPE_SESSION_REQUIRED",
                category="learning",
                message="Session Context requires a stable trusted session_id.",
                retryable=False,
                details={"field": "session_id", "store": "session_context"},
            )
        if policy.consent_required and policy.personal_stores and not consented:
            raise HarnessError(
                code="LEARNING_CONSENT_REQUIRED",
                category="learning",
                message="This personal learning policy requires explicit host consent.",
                retryable=False,
                details={"stores": list(policy.personal_stores)},
            )

        identity = {
            "tenant_id": context.tenant_id,
            "org_id": context.org_id,
            "agent_id": agent_id,
        }
        storage_namespace = _storage_key(
            "namespace",
            {**identity, "namespace": policy.namespace},
        )
        storage_user_id = (
            _storage_key("user", {**identity, "user_id": context.user_id})
            if context.user_id is not None
            else None
        )
        storage_session_id = (
            _storage_key(
                "session",
                {
                    **identity,
                    "user_id": context.user_id,
                    "session_id": context.session_id,
                },
            )
            if context.session_id is not None
            else None
        )
        return cls(
            tenant_id=context.tenant_id,
            org_id=context.org_id,
            agent_id=agent_id,
            user_id=context.user_id,
            session_id=context.session_id,
            namespace=policy.namespace,
            storage_namespace=storage_namespace,
            storage_user_id=storage_user_id,
            storage_session_id=storage_session_id,
            retention_days=policy.retention_days,
            consented=consented,
        )

    def descriptor(self) -> dict[str, Any]:
        """Return safe storage scope metadata without raw tenant/user/session IDs."""
        return {
            "schema_version": 1,
            "agent_id": self.agent_id,
            "namespace": self.namespace,
            "storage_namespace": self.storage_namespace,
            "storage_user_id": self.storage_user_id,
            "storage_session_id": self.storage_session_id,
            "retention_days": self.retention_days,
            "consented": self.consented,
        }


__all__ = [
    "LearningMode",
    "LearningPolicy",
    "LearningProfile",
    "LearningPromotion",
    "LearningScope",
    "LearningStorePolicy",
    "LearningWritePath",
]
