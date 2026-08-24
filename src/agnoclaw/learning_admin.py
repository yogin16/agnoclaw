"""Truthful scoped administration for direct personal and session learning stores."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from .learning import LearningPolicy, LearningScope
from .runtime.errors import HarnessError
from .runtime.security import freeze_data, thaw_data

LEARNING_ADMIN_SCHEMA_VERSION = "1.0"
MAX_LEARNING_ADMIN_CONTENT_BYTES = 1_048_576
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _require_id(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    if len(value) > 512:
        raise ValueError(f"{field_name} cannot exceed 512 characters")


def _require_digest(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a canonical sha256 digest")


def _require_timestamp(value: str, *, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        thaw_data(freeze_data(value)),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _digest(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(value).encode('utf-8')).hexdigest()}"


class LearningDataStore(StrEnum):
    USER_PROFILE = "user_profile"
    USER_MEMORY = "user_memory"
    SESSION_CONTEXT = "session_context"


class LearningMutationAction(StrEnum):
    REPLACE = "replace"
    FORGET = "forget"


class LearningVerificationLevel(StrEnum):
    POINT_IN_TIME = "point_in_time"


@dataclass(frozen=True, slots=True)
class LearningDataRecord:
    store: LearningDataStore
    scope_digest: str
    present: bool
    content: Any = None
    content_digest: str | None = None
    observed_at: str = field(default_factory=_now)
    schema_version: str = LEARNING_ADMIN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "store", LearningDataStore(self.store))
        if self.schema_version != LEARNING_ADMIN_SCHEMA_VERSION:
            raise ValueError("unsupported learning administration schema")
        _require_digest(self.scope_digest, field_name="scope_digest")
        _require_timestamp(self.observed_at, field_name="observed_at")
        if not isinstance(self.present, bool):
            raise TypeError("present must be a boolean")
        object.__setattr__(self, "content", freeze_data(self.content))
        if self.present:
            if not isinstance(thaw_data(self.content), dict):
                raise TypeError("present learning content must be an object")
            expected = _digest(thaw_data(self.content))
            if self.content_digest != expected:
                raise ValueError("content_digest does not match learning content")
        elif self.content is not None or self.content_digest is not None:
            raise ValueError("absent learning data cannot contain content")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "store": self.store.value,
            "scope_digest": self.scope_digest,
            "present": self.present,
            "content": thaw_data(self.content),
            "content_digest": self.content_digest,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True, slots=True)
class LearningMutationReceipt:
    operation_id: str
    action: LearningMutationAction
    store: LearningDataStore
    scope_digest: str
    existed_before: bool
    content_digest_before: str | None
    content_digest_after: str | None
    backend_delete_confirmed: bool | None
    verified: bool
    verification_level: LearningVerificationLevel = LearningVerificationLevel.POINT_IN_TIME
    completed_at: str = field(default_factory=_now)
    schema_version: str = LEARNING_ADMIN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_id(self.operation_id, field_name="operation_id")
        if self.schema_version != LEARNING_ADMIN_SCHEMA_VERSION:
            raise ValueError("unsupported learning administration schema")
        object.__setattr__(self, "action", LearningMutationAction(self.action))
        object.__setattr__(self, "store", LearningDataStore(self.store))
        object.__setattr__(
            self,
            "verification_level",
            LearningVerificationLevel(self.verification_level),
        )
        _require_digest(self.scope_digest, field_name="scope_digest")
        _require_timestamp(self.completed_at, field_name="completed_at")
        if not isinstance(self.existed_before, bool) or not isinstance(
            self.verified,
            bool,
        ):
            raise TypeError("receipt flags must be booleans")
        if self.backend_delete_confirmed is not None and not isinstance(
            self.backend_delete_confirmed,
            bool,
        ):
            raise TypeError("backend_delete_confirmed must be a boolean or None")
        if not self.verified:
            raise ValueError("unverified learning mutations cannot produce receipts")
        if self.existed_before:
            if self.content_digest_before is None:
                raise ValueError("an existing prior record requires its content digest")
            _require_digest(
                self.content_digest_before,
                field_name="content_digest_before",
            )
        elif self.content_digest_before is not None:
            raise ValueError("an absent prior record cannot have a content digest")
        if self.action is LearningMutationAction.FORGET:
            if self.content_digest_after is not None:
                raise ValueError("a verified forget must have no content afterward")
            if self.backend_delete_confirmed is None:
                raise ValueError("forget receipts require the backend delete result")
        else:
            if self.content_digest_after is None:
                raise ValueError("a verified replace requires its resulting content digest")
            _require_digest(
                self.content_digest_after,
                field_name="content_digest_after",
            )
            if self.backend_delete_confirmed is not None:
                raise ValueError("replace receipts cannot contain a delete result")

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "operation_id": self.operation_id,
            "action": self.action.value,
            "store": self.store.value,
            "scope_digest": self.scope_digest,
            "existed_before": self.existed_before,
            "content_digest_before": self.content_digest_before,
            "content_digest_after": self.content_digest_after,
            "backend_delete_confirmed": self.backend_delete_confirmed,
            "verified": self.verified,
            "verification_level": self.verification_level.value,
            "completed_at": self.completed_at,
        }


@dataclass(frozen=True, slots=True)
class _StoreBinding:
    name: LearningDataStore
    store: Any
    db: Any
    record_id: str
    query: Any
    upsert: Any
    scope_digest: str


class LearningAdminBackendError(HarnessError):
    def __init__(self, *, operation: str, store: LearningDataStore) -> None:
        super().__init__(
            code="LEARNING_ADMIN_BACKEND_FAILED",
            category="learning",
            message="The learning backend could not complete the administrative operation.",
            retryable=True,
            details={"operation": operation, "store": store.value},
        )


class LearningAdminGateway:
    """Read/replace/forget exact Agno identity-keyed stores with verification."""

    def __init__(
        self,
        machine: Any,
        *,
        policy: LearningPolicy,
        scope: LearningScope,
        agent_id: str,
    ) -> None:
        if machine is None:
            raise TypeError("machine cannot be None")
        if not isinstance(policy, LearningPolicy):
            raise TypeError("policy must be a LearningPolicy")
        if not isinstance(scope, LearningScope):
            raise TypeError("scope must be a LearningScope")
        _require_id(agent_id, field_name="agent_id")
        self.machine = machine
        self.policy = policy
        self.scope = scope
        self.agent_id = agent_id

    @staticmethod
    async def _call(
        operation: str,
        store: LearningDataStore,
        method: Callable[..., Any],
        /,
        **kwargs: Any,
    ) -> Any:
        try:
            if inspect.iscoroutinefunction(method):
                return await method(**kwargs)
            result = await asyncio.to_thread(method, **kwargs)
            return await result if inspect.isawaitable(result) else result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise LearningAdminBackendError(operation=operation, store=store) from exc

    def _binding(self, store: LearningDataStore) -> _StoreBinding:
        store = LearningDataStore(store)
        store_policy = getattr(self.policy, store.value)
        if store_policy is None:
            raise HarnessError(
                code="LEARNING_ADMIN_STORE_DISABLED",
                category="learning",
                message=f"{store.value} is not enabled by this learning policy.",
                retryable=False,
                details={"store": store.value},
            )
        store_object = getattr(self.machine, f"{store.value}_store", None)
        if store_object is None:
            raise HarnessError(
                code="LEARNING_ADMIN_STORE_UNAVAILABLE",
                category="learning",
                message="The configured Agno learning store is unavailable.",
                retryable=False,
                details={"store": store.value},
            )
        db = getattr(store_object, "db", None)
        if db is None:
            raise HarnessError(
                code="LEARNING_ADMIN_DATABASE_REQUIRED",
                category="learning",
                message="The configured learning store has no administrative database.",
                retryable=False,
                details={"store": store.value},
            )
        query: dict[str, Any]
        upsert: dict[str, Any]
        identity_payload: dict[str, str | None]
        if store in {LearningDataStore.USER_PROFILE, LearningDataStore.USER_MEMORY}:
            identity = self.scope.storage_user_id
            if identity is None:  # pragma: no cover - LearningScope invariant
                raise AssertionError("personal store without storage user identity")
            prefix = "user_profile" if store is LearningDataStore.USER_PROFILE else "memories"
            record_id = f"{prefix}_{identity}"
            query = {"learning_type": store.value, "user_id": identity}
            upsert = {
                "id": record_id,
                "learning_type": store.value,
                "user_id": identity,
                "agent_id": self.agent_id,
            }
            identity_payload = {"user_id": identity}
        else:
            identity = self.scope.storage_session_id
            if identity is None:  # pragma: no cover - LearningScope invariant
                raise AssertionError("session store without storage session identity")
            record_id = f"session_context_{identity}"
            query = {"learning_type": store.value, "session_id": identity}
            upsert = {
                "id": record_id,
                "learning_type": store.value,
                "session_id": identity,
                "user_id": self.scope.storage_user_id,
                "agent_id": self.agent_id,
            }
            identity_payload = {
                "session_id": identity,
                "user_id": self.scope.storage_user_id,
            }
        return _StoreBinding(
            name=store,
            store=store_object,
            db=db,
            record_id=record_id,
            query=freeze_data(query),
            upsert=freeze_data(upsert),
            scope_digest=_digest(
                {
                    "store": store.value,
                    "tenant_id": self.scope.tenant_id,
                    **identity_payload,
                }
            ),
        )

    async def _read_binding(self, binding: _StoreBinding) -> LearningDataRecord:
        method = getattr(binding.db, "get_learning", None)
        if not callable(method):
            raise HarnessError(
                code="LEARNING_ADMIN_CRUD_UNAVAILABLE",
                category="learning",
                message="The Agno database lacks the required learning CRUD contract.",
                retryable=False,
                details={"operation": "get_learning", "store": binding.name.value},
            )
        raw = await self._call(
            "read",
            binding.name,
            method,
            **thaw_data(binding.query),
        )
        if raw is None:
            return LearningDataRecord(
                store=binding.name,
                scope_digest=binding.scope_digest,
                present=False,
            )
        if not isinstance(raw, dict) or not isinstance(raw.get("content"), dict):
            raise HarnessError(
                code="LEARNING_ADMIN_RECORD_INVALID",
                category="learning",
                message="The learning backend returned an invalid record shape.",
                retryable=False,
                details={"store": binding.name.value},
            )
        content = raw["content"]
        try:
            return LearningDataRecord(
                store=binding.name,
                scope_digest=binding.scope_digest,
                present=True,
                content=content,
                content_digest=_digest(content),
            )
        except (TypeError, ValueError) as exc:
            raise HarnessError(
                code="LEARNING_ADMIN_RECORD_INVALID",
                category="learning",
                message="The learning backend returned invalid record content.",
                retryable=False,
                details={"store": binding.name.value},
            ) from exc

    async def read(self, store: LearningDataStore) -> LearningDataRecord:
        return await self._read_binding(self._binding(store))

    @staticmethod
    def _validate_builtin_shape(
        store: LearningDataStore,
        normalized: dict[str, Any],
    ) -> None:
        invalid = False
        if store is LearningDataStore.USER_PROFILE:
            invalid = any(
                value is not None and not isinstance(value, str)
                for value in (
                    normalized.get("name"),
                    normalized.get("preferred_name"),
                )
            )
        elif store is LearningDataStore.USER_MEMORY:
            memories = normalized.get("memories")
            invalid = False
            if isinstance(memories, list):
                memory_ids: list[str] = []
                for memory in memories:
                    if not isinstance(memory, dict):
                        invalid = True
                        break
                    memory_id = memory.get("id")
                    content = memory.get("content")
                    if (
                        not isinstance(memory_id, str)
                        or not memory_id.strip()
                        or len(memory_id) > 512
                        or not isinstance(content, str)
                        or not content.strip()
                    ):
                        invalid = True
                        break
                    memory_ids.append(memory_id)
                invalid = invalid or len(memory_ids) != len(set(memory_ids))
            else:
                invalid = True
        else:
            invalid = any(
                normalized.get(name) is not None and not isinstance(normalized.get(name), expected)
                for name, expected in (
                    ("summary", str),
                    ("goal", str),
                    ("plan", list),
                    ("progress", list),
                )
            )
            for field_name in ("plan", "progress"):
                values = normalized.get(field_name)
                if isinstance(values, list) and any(
                    not isinstance(item, str) or not item.strip() for item in values
                ):
                    invalid = True

        if invalid:
            raise HarnessError(
                code="LEARNING_ADMIN_CONTENT_INVALID",
                category="learning",
                message="Replacement content violates the direct store contract.",
                retryable=False,
                details={"store": store.value},
            )

    def _validated_content(
        self,
        binding: _StoreBinding,
        content: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(content, dict) or not content:
            raise HarnessError(
                code="LEARNING_ADMIN_CONTENT_INVALID",
                category="learning",
                message="Replacement learning content must be a non-empty object.",
                retryable=False,
                details={"store": binding.name.value},
            )
        internal = {"agent_id", "team_id", "created_at", "updated_at"}
        identity = {"user_id", "session_id"}
        forbidden = sorted(set(content) & (internal | identity))
        if forbidden:
            raise HarnessError(
                code="LEARNING_ADMIN_CONTENT_IDENTITY_FORBIDDEN",
                category="learning",
                message="Learning identity and audit fields are supplied by the harness.",
                retryable=False,
                details={"store": binding.name.value, "fields": forbidden},
            )
        payload = dict(content)
        if binding.name in {
            LearningDataStore.USER_PROFILE,
            LearningDataStore.USER_MEMORY,
        }:
            payload["user_id"] = thaw_data(binding.query)["user_id"]
        else:
            payload["session_id"] = thaw_data(binding.query)["session_id"]
            payload["user_id"] = thaw_data(binding.upsert).get("user_id")
        schema = getattr(binding.store, "schema", None)
        parser = getattr(schema, "from_dict", None)
        if schema is None or not callable(parser):
            raise HarnessError(
                code="LEARNING_ADMIN_SCHEMA_UNAVAILABLE",
                category="learning",
                message="The Agno store lacks a verifiable data schema.",
                retryable=False,
                details={"store": binding.name.value},
            )
        try:
            parsed = parser(payload)
            serializer = getattr(parsed, "to_dict", None)
            normalized = serializer() if callable(serializer) else None
        except Exception as exc:
            raise HarnessError(
                code="LEARNING_ADMIN_CONTENT_INVALID",
                category="learning",
                message="Replacement content does not satisfy the Agno store schema.",
                retryable=False,
                details={"store": binding.name.value},
            ) from exc
        if parsed is None or not isinstance(normalized, dict) or not normalized:
            raise HarnessError(
                code="LEARNING_ADMIN_CONTENT_INVALID",
                category="learning",
                message="Replacement content does not satisfy the Agno store schema.",
                retryable=False,
                details={"store": binding.name.value},
            )
        self._validate_builtin_shape(binding.name, normalized)
        dropped = sorted(set(content) - set(normalized))
        if dropped:
            raise HarnessError(
                code="LEARNING_ADMIN_CONTENT_FIELDS_UNSUPPORTED",
                category="learning",
                message="The store schema does not accept one or more content fields.",
                retryable=False,
                details={"store": binding.name.value, "fields": dropped},
            )
        try:
            size = len(_canonical_json(normalized).encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise HarnessError(
                code="LEARNING_ADMIN_CONTENT_INVALID",
                category="learning",
                message="Replacement content is not bounded JSON data.",
                retryable=False,
                details={"store": binding.name.value},
            ) from exc
        if size > MAX_LEARNING_ADMIN_CONTENT_BYTES:
            raise HarnessError(
                code="LEARNING_ADMIN_CONTENT_TOO_LARGE",
                category="learning",
                message="Replacement learning content exceeds the administrative limit.",
                retryable=False,
                details={
                    "store": binding.name.value,
                    "size_bytes": size,
                    "limit_bytes": MAX_LEARNING_ADMIN_CONTENT_BYTES,
                },
            )
        return normalized

    async def replace(
        self,
        store: LearningDataStore,
        content: dict[str, Any],
        *,
        operation_id: str,
    ) -> LearningMutationReceipt:
        _require_id(operation_id, field_name="operation_id")
        binding = self._binding(store)
        before = await self._read_binding(binding)
        normalized = self._validated_content(binding, content)
        method = getattr(binding.db, "upsert_learning", None)
        if not callable(method):
            raise HarnessError(
                code="LEARNING_ADMIN_CRUD_UNAVAILABLE",
                category="learning",
                message="The Agno database lacks the required learning CRUD contract.",
                retryable=False,
                details={"operation": "upsert_learning", "store": binding.name.value},
            )
        await self._call(
            "replace",
            binding.name,
            method,
            **thaw_data(binding.upsert),
            content=normalized,
        )
        after = await self._read_binding(binding)
        expected_digest = _digest(normalized)
        if not after.present or after.content_digest != expected_digest:
            raise HarnessError(
                code="LEARNING_ADMIN_REPLACE_NOT_VERIFIED",
                category="learning",
                message="The replacement could not be verified after the backend write.",
                retryable=True,
                details={"store": binding.name.value, "operation_id": operation_id},
            )
        return LearningMutationReceipt(
            operation_id=operation_id,
            action=LearningMutationAction.REPLACE,
            store=binding.name,
            scope_digest=binding.scope_digest,
            existed_before=before.present,
            content_digest_before=before.content_digest,
            content_digest_after=after.content_digest,
            backend_delete_confirmed=None,
            verified=True,
        )

    async def forget(
        self,
        store: LearningDataStore,
        *,
        operation_id: str,
    ) -> LearningMutationReceipt:
        _require_id(operation_id, field_name="operation_id")
        binding = self._binding(store)
        before = await self._read_binding(binding)
        method = getattr(binding.db, "delete_learning", None)
        if not callable(method):
            raise HarnessError(
                code="LEARNING_ADMIN_CRUD_UNAVAILABLE",
                category="learning",
                message="The Agno database lacks the required learning CRUD contract.",
                retryable=False,
                details={"operation": "delete_learning", "store": binding.name.value},
            )
        deleted = await self._call(
            "forget",
            binding.name,
            method,
            id=binding.record_id,
        )
        after = await self._read_binding(binding)
        if after.present:
            raise HarnessError(
                code="LEARNING_ADMIN_FORGET_NOT_VERIFIED",
                category="learning",
                message="The learning remained visible after the backend delete.",
                retryable=True,
                details={"store": binding.name.value, "operation_id": operation_id},
            )
        return LearningMutationReceipt(
            operation_id=operation_id,
            action=LearningMutationAction.FORGET,
            store=binding.name,
            scope_digest=binding.scope_digest,
            existed_before=before.present,
            content_digest_before=before.content_digest,
            content_digest_after=None,
            backend_delete_confirmed=deleted is True,
            verified=True,
        )


__all__ = [
    "LEARNING_ADMIN_SCHEMA_VERSION",
    "LearningAdminBackendError",
    "LearningAdminGateway",
    "LearningDataRecord",
    "LearningDataStore",
    "LearningMutationAction",
    "LearningMutationReceipt",
    "LearningVerificationLevel",
    "MAX_LEARNING_ADMIN_CONTENT_BYTES",
]
