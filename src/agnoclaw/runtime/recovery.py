"""Bounded, owner-scoped startup recovery for durable harness runs."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from .children import (
    MAX_CHILD_DEPTH,
    ChildRunContractError,
    ChildRunSpec,
)
from .context import ExecutionContext
from .errors import HarnessError
from .leases import RuntimeLeaseUnavailableError
from .lifecycle import RunNotFoundError, RunSnapshot, RunState
from .run_handle import HarnessRun
from .store import MAX_RECOVERY_MINIMUM_AGE_SECONDS, RunOwner, RuntimeStore

RECOVERY_CURSOR_VERSION = 1
MAX_RECOVERY_BATCH_SIZE = 100
MAX_RECOVERY_CONCURRENCY = 32


class RuntimeRecoveryCursorError(HarnessError):
    """Raised when a recovery cursor is malformed or belongs to another owner."""

    def __init__(self) -> None:
        super().__init__(
            code="RUN_RECOVERY_CURSOR_INVALID",
            category="recovery",
            message="The recovery cursor is invalid for this owner.",
            retryable=False,
        )


class RuntimeRecoveryStatus(StrEnum):
    """Safe per-run outcome from one bounded startup scan."""

    RECOVERED = "recovered"
    LEASE_BUSY = "lease_busy"
    FAILED = "failed"


@dataclass(frozen=True)
class RuntimeRecoveryItem:
    """One content-minimized recovery outcome."""

    run_id: str
    status: RuntimeRecoveryStatus
    state: RunState
    error_code: str | None = None


@dataclass(frozen=True)
class RuntimeRecoveryBatch:
    """Ordered outcomes and an owner-bound cursor for the next page."""

    items: tuple[RuntimeRecoveryItem, ...]
    next_cursor: str | None

    @property
    def recovered(self) -> int:
        return sum(item.status is RuntimeRecoveryStatus.RECOVERED for item in self.items)

    @property
    def lease_busy(self) -> int:
        return sum(item.status is RuntimeRecoveryStatus.LEASE_BUSY for item in self.items)

    @property
    def failed(self) -> int:
        return sum(item.status is RuntimeRecoveryStatus.FAILED for item in self.items)


@dataclass(frozen=True)
class ChildRecoveryContext:
    """Durable child authority and ancestry needed for safe continuation."""

    spec: ChildRunSpec | None = None
    terminal_ancestor: RunSnapshot | None = None
    error: ChildRunContractError | None = None


RecoverRun = Callable[..., Awaitable[HarnessRun]]


def _child_authority_is_valid(
    spec: ChildRunSpec,
    *,
    governing: ChildRunSpec | None,
) -> bool:
    budget = spec.budget
    parent_budget = governing.budget if governing is not None else budget
    return (
        spec.depth <= parent_budget.max_depth
        and budget.max_depth <= parent_budget.max_depth
        and budget.max_fanout <= parent_budget.max_fanout
        and budget.timeout_seconds <= parent_budget.timeout_seconds
        and budget.max_tokens <= parent_budget.max_tokens
        and budget.max_cost_microusd <= parent_budget.max_cost_microusd
        and (
            governing is None
            or (
                set(spec.capability_allowlist).issubset(governing.capability_allowlist)
                and (not spec.learning_allowed or governing.learning_allowed)
            )
        )
    )


def inspect_child_recovery(
    store: RuntimeStore,
    snapshot: RunSnapshot,
    *,
    owner: RunOwner,
) -> ChildRecoveryContext:
    """Validate the whole persisted child chain before any restart dispatch."""
    if snapshot.parent_run_id is None:
        return ChildRecoveryContext()
    try:
        first_spec: ChildRunSpec | None = None
        terminal_ancestor: RunSnapshot | None = None
        seen = {snapshot.run_id}
        current = snapshot
        for _depth in range(MAX_CHILD_DEPTH + 1):
            if current.parent_run_id is None:
                break
            spec = store.get_child_spec(current.run_id, owner=owner)
            first_spec = first_spec or spec
            parent = store.get_run(current.parent_run_id, owner=owner)
            expected_root = parent.root_run_id or parent.run_id
            if (
                spec.child_run_id != current.run_id
                or spec.parent_run_id != current.parent_run_id
                or spec.root_run_id != current.root_run_id
                or spec.depth != current.child_depth
                or current.child_depth != parent.child_depth + 1
                or current.root_run_id != expected_root
                or parent.run_id in seen
            ):
                raise ValueError("persisted child lineage does not form one acyclic chain")
            governing = (
                store.get_child_spec(parent.run_id, owner=owner)
                if parent.parent_run_id is not None
                else None
            )
            if not _child_authority_is_valid(spec, governing=governing):
                raise ValueError("persisted child authority escalates its parent grant")
            if terminal_ancestor is None and parent.terminal:
                terminal_ancestor = parent
            seen.add(parent.run_id)
            current = parent
        else:
            raise ValueError("persisted child lineage exceeds the maximum depth")
        if current.parent_run_id is not None or snapshot.root_run_id != current.run_id:
            raise ValueError("persisted child root does not terminate the lineage")
        return ChildRecoveryContext(
            spec=first_spec,
            terminal_ancestor=terminal_ancestor,
        )
    except (ChildRunContractError, RunNotFoundError, TypeError, ValueError) as exc:
        return ChildRecoveryContext(
            error=ChildRunContractError(
                code="CHILD_RECOVERY_LINEAGE_INVALID",
                message="The persisted child lineage cannot be certified for restart.",
                details={
                    "child_run_id": snapshot.run_id,
                    "cause_code": getattr(exc, "code", "CHILD_LINEAGE_INVALID"),
                },
            )
        )


def _owner_scope(owner: RunOwner) -> str:
    canonical = json.dumps(
        {"tenant_id": owner.tenant_id, "user_id": owner.user_id},
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _encode_cursor(*, owner: RunOwner, after_run_id: str) -> str:
    payload = json.dumps(
        {"v": RECOVERY_CURSOR_VERSION, "scope": _owner_scope(owner), "after": after_run_id},
        separators=(",", ":"),
        sort_keys=True,
    )
    token = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    return f"recovery_v1_{token}"


def _decode_cursor(cursor: str | None, *, owner: RunOwner) -> str | None:
    if cursor is None:
        return None
    try:
        if (
            not isinstance(cursor, str)
            or not cursor.startswith("recovery_v1_")
            or len(cursor) > 4096
        ):
            raise ValueError
        token = cursor.removeprefix("recovery_v1_")
        padded = token + "=" * (-len(token) % 4)
        payload = json.loads(
            base64.b64decode(padded, altchars=b"-_", validate=True).decode("utf-8")
        )
        if (
            not isinstance(payload, dict)
            or payload.get("v") != RECOVERY_CURSOR_VERSION
            or payload.get("scope") != _owner_scope(owner)
            or not isinstance(payload.get("after"), str)
            or not payload["after"].strip()
        ):
            raise ValueError
        return payload["after"]
    except (binascii.Error, TypeError, ValueError, UnicodeDecodeError) as exc:
        raise RuntimeRecoveryCursorError() from exc


async def recover_pending_runs(
    *,
    store: RuntimeStore,
    recover_run: RecoverRun,
    default_owner: RunOwner,
    context: ExecutionContext | None = None,
    cursor: str | None = None,
    limit: int = 25,
    concurrency: int = 4,
    minimum_age_seconds: int = 30,
) -> RuntimeRecoveryBatch:
    """Claim and classify one bounded page of executable runs.

    The store query always uses an exact owner tuple. Intentional wait and pause states
    are excluded by the store and are never woken by startup scanning.
    """
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= MAX_RECOVERY_BATCH_SIZE
    ):
        raise ValueError(f"limit must be between 1 and {MAX_RECOVERY_BATCH_SIZE}")
    if (
        not isinstance(concurrency, int)
        or isinstance(concurrency, bool)
        or not 1 <= concurrency <= MAX_RECOVERY_CONCURRENCY
    ):
        raise ValueError(f"concurrency must be between 1 and {MAX_RECOVERY_CONCURRENCY}")
    if (
        not isinstance(minimum_age_seconds, int)
        or isinstance(minimum_age_seconds, bool)
        or not 0 <= minimum_age_seconds <= MAX_RECOVERY_MINIMUM_AGE_SECONDS
    ):
        raise ValueError("minimum_age_seconds must be between 0 and 86400")
    owner = (
        RunOwner(tenant_id=context.tenant_id, user_id=context.user_id)
        if context is not None
        else default_owner
    )
    after_run_id = _decode_cursor(cursor, owner=owner)
    candidates = await asyncio.to_thread(
        store.list_recoverable_runs,
        owner=owner,
        after_run_id=after_run_id,
        minimum_age_seconds=minimum_age_seconds,
        limit=limit + 1,
    )
    page = candidates[:limit]
    next_cursor = (
        _encode_cursor(owner=owner, after_run_id=page[-1].run_id)
        if len(candidates) > limit
        else None
    )
    gate = asyncio.Semaphore(min(concurrency, limit))

    async def recover(candidate: RunSnapshot) -> RuntimeRecoveryItem:
        async with gate:
            try:
                handle = await recover_run(candidate.run_id, context=context)
                current = await handle.status()
            except asyncio.CancelledError:
                raise
            except RuntimeLeaseUnavailableError:
                return RuntimeRecoveryItem(
                    run_id=candidate.run_id,
                    status=RuntimeRecoveryStatus.LEASE_BUSY,
                    state=candidate.state,
                    error_code="RUNTIME_LEASE_UNAVAILABLE",
                )
            except Exception as exc:
                return RuntimeRecoveryItem(
                    run_id=candidate.run_id,
                    status=RuntimeRecoveryStatus.FAILED,
                    state=candidate.state,
                    error_code=(
                        exc.code if isinstance(exc, HarnessError) else "RUN_RECOVERY_FAILED"
                    ),
                )
            return RuntimeRecoveryItem(
                run_id=candidate.run_id,
                status=RuntimeRecoveryStatus.RECOVERED,
                state=current.state,
            )

    items = tuple(await asyncio.gather(*(recover(candidate) for candidate in page)))
    return RuntimeRecoveryBatch(items=items, next_cursor=next_cursor)


__all__ = [
    "ChildRecoveryContext",
    "MAX_RECOVERY_BATCH_SIZE",
    "MAX_RECOVERY_CONCURRENCY",
    "RECOVERY_CURSOR_VERSION",
    "RuntimeRecoveryBatch",
    "RuntimeRecoveryCursorError",
    "RuntimeRecoveryItem",
    "RuntimeRecoveryStatus",
    "inspect_child_recovery",
    "recover_pending_runs",
]
