"""Owner-authorized administration for quarantined runtime outbox events."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from .context import ExecutionContext
from .errors import HarnessError
from .store import (
    DeadLetterAuditRecord,
    DeadLetterItem,
    DeadLetterRequeueDecision,
    RunOwner,
    RuntimeStore,
)

DEAD_LETTER_INSPECT_SCOPE = "runtime.dead_letters.inspect"
DEAD_LETTER_REQUEUE_SCOPE = "runtime.dead_letters.requeue"
DEAD_LETTER_AUDIT_SCOPE = "runtime.dead_letters.audit"
MAX_DEAD_LETTER_ADMIN_PAGE_SIZE = 100

_T = TypeVar("_T")


class DeadLetterAdminAuthorizationError(HarnessError):
    def __init__(self, *, scope: str) -> None:
        super().__init__(
            code="OUTBOX_DEAD_LETTER_ADMIN_DENIED",
            category="authorization",
            message="Dead-letter administration requires trusted in-scope authority.",
            retryable=False,
            details={"required_scope": scope},
        )


class DeadLetterAdminCursorError(HarnessError):
    def __init__(self) -> None:
        super().__init__(
            code="OUTBOX_DEAD_LETTER_CURSOR_INVALID",
            category="runtime_store",
            message="The dead-letter cursor is invalid for this owner and view.",
            retryable=False,
        )


@dataclass(frozen=True, slots=True)
class DeadLetterPage:
    items: tuple[DeadLetterItem, ...]
    audit: DeadLetterAuditRecord
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class DeadLetterAuditPage:
    items: tuple[DeadLetterAuditRecord, ...]
    next_cursor: str | None


def _canonical_digest(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _owner_digest(owner: RunOwner) -> str:
    return _canonical_digest({"tenant_id": owner.tenant_id, "user_id": owner.user_id})


def _operator_digest(context: ExecutionContext) -> str:
    return _canonical_digest(
        {
            "tenant_id": context.tenant_id,
            "user_id": context.user_id,
            "identity_source": context.identity_source.value,
        }
    )


def _authority_digest(context: ExecutionContext) -> str:
    return _canonical_digest(
        {
            "operator": _operator_digest(context),
            "roles": sorted(set(context.roles)),
            "scopes": sorted(set(context.scopes)),
            "admission_authority": (
                context.admission.authority_digest if context.admission is not None else None
            ),
        }
    )


def _authorize(
    context: ExecutionContext,
    *,
    scope: str,
    owner: RunOwner | None,
) -> RunOwner:
    resolved = owner or RunOwner(context.tenant_id, context.user_id)
    if (
        not context.identity_source.authoritative
        or context.user_id is None
        or scope not in context.scopes
        or context.tenant_id != resolved.tenant_id
        or (context.tenant_id is None and context.user_id != resolved.user_id)
    ):
        raise DeadLetterAdminAuthorizationError(scope=scope)
    return resolved


def _encode_cursor(*, view: str, owner: RunOwner, after: int) -> str:
    payload = json.dumps(
        {"v": 1, "view": view, "owner": _owner_digest(owner), "after": after},
        separators=(",", ":"),
        sort_keys=True,
    )
    token = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"dead_letter_v1_{token}"


def _decode_cursor(cursor: str | None, *, view: str, owner: RunOwner) -> int:
    if cursor is None:
        return 0
    try:
        prefix = "dead_letter_v1_"
        if not isinstance(cursor, str) or not cursor.startswith(prefix):
            raise ValueError
        token = cursor.removeprefix(prefix)
        payload = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        after = payload["after"]
        if (
            payload.get("v") != 1
            or payload.get("view") != view
            or payload.get("owner") != _owner_digest(owner)
            or isinstance(after, bool)
            or not isinstance(after, int)
            or after < 0
        ):
            raise ValueError
        return after
    except Exception as exc:
        raise DeadLetterAdminCursorError() from exc


async def _commit_store_call(call: Callable[[], _T]) -> _T:
    task = asyncio.create_task(asyncio.to_thread(call))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        try:
            await asyncio.shield(task)
        except Exception:
            # Cancellation remains the caller-visible outcome. The retry-safe mutation
            # contract reveals whether the authoritative store committed or rolled back.
            pass
        raise cancelled


class RuntimeDeadLetterAdmin:
    """Small host API for scoped inspection, replay, and immutable audit history."""

    def __init__(self, store: RuntimeStore) -> None:
        if not isinstance(store, RuntimeStore):
            raise TypeError("store must implement RuntimeStore")
        self._store = store

    async def inspect(
        self,
        *,
        context: ExecutionContext,
        reason_code: str,
        owner: RunOwner | None = None,
        cursor: str | None = None,
        limit: int = 25,
    ) -> DeadLetterPage:
        if not 1 <= limit <= MAX_DEAD_LETTER_ADMIN_PAGE_SIZE:
            raise ValueError("limit must be between 1 and 100")
        resolved = _authorize(context, scope=DEAD_LETTER_INSPECT_SCOPE, owner=owner)
        after = _decode_cursor(cursor, view="dead_letters", owner=resolved)
        decision = await _commit_store_call(
            lambda: self._store.inspect_dead_letters(
                owner=resolved,
                operator_digest=_operator_digest(context),
                authority_digest=_authority_digest(context),
                reason_code=reason_code,
                after_outbox_id=after,
                limit=limit,
            )
        )
        next_cursor = None
        if len(decision.items) == limit:
            next_cursor = _encode_cursor(
                view="dead_letters",
                owner=resolved,
                after=decision.items[-1].outbox_id,
            )
        return DeadLetterPage(
            items=decision.items,
            audit=decision.audit,
            next_cursor=next_cursor,
        )

    async def requeue(
        self,
        item: DeadLetterItem,
        *,
        context: ExecutionContext,
        reason_code: str,
        mutation_id: str,
        owner: RunOwner | None = None,
        delay_seconds: float = 0,
    ) -> DeadLetterRequeueDecision:
        resolved = _authorize(context, scope=DEAD_LETTER_REQUEUE_SCOPE, owner=owner)
        return await _commit_store_call(
            lambda: self._store.requeue_dead_letter(
                owner=resolved,
                operator_digest=_operator_digest(context),
                authority_digest=_authority_digest(context),
                reason_code=reason_code,
                mutation_id=mutation_id,
                outbox_id=item.outbox_id,
                expected_dead_lettered_at=item.dead_lettered_at,
                delay_seconds=delay_seconds,
            )
        )

    async def audit_history(
        self,
        *,
        context: ExecutionContext,
        owner: RunOwner | None = None,
        cursor: str | None = None,
        limit: int = 25,
    ) -> DeadLetterAuditPage:
        if not 1 <= limit <= MAX_DEAD_LETTER_ADMIN_PAGE_SIZE:
            raise ValueError("limit must be between 1 and 100")
        resolved = _authorize(context, scope=DEAD_LETTER_AUDIT_SCOPE, owner=owner)
        after = _decode_cursor(cursor, view="dead_letter_audit", owner=resolved)
        items = await asyncio.to_thread(
            self._store.list_dead_letter_audit,
            owner=resolved,
            after_audit_sequence=after,
            limit=limit,
        )
        next_cursor = None
        if len(items) == limit:
            next_cursor = _encode_cursor(
                view="dead_letter_audit",
                owner=resolved,
                after=items[-1].audit_sequence,
            )
        return DeadLetterAuditPage(items=tuple(items), next_cursor=next_cursor)


__all__ = [
    "DEAD_LETTER_AUDIT_SCOPE",
    "DEAD_LETTER_INSPECT_SCOPE",
    "DEAD_LETTER_REQUEUE_SCOPE",
    "DeadLetterAdminAuthorizationError",
    "DeadLetterAdminCursorError",
    "DeadLetterAuditPage",
    "DeadLetterPage",
    "RuntimeDeadLetterAdmin",
]
