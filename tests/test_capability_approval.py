from __future__ import annotations

import asyncio
import inspect

import pytest

from agnoclaw.capabilities import (
    CapabilityConcurrency,
    CapabilityKind,
    CapabilityLifetime,
    CapabilityRecovery,
    CapabilitySpec,
    CapabilityTrust,
)
from agnoclaw.capability_approval import DurableApprovalCoordinator
from agnoclaw.runtime.approvals import ApprovalState
from agnoclaw.runtime.context import ExecutionContext
from agnoclaw.runtime.errors import HarnessError
from agnoclaw.runtime.hooks import ToolCallRequest
from agnoclaw.runtime.lifecycle import (
    LifecycleTransition,
    RunSnapshot,
    RunState,
    TransitionKind,
)
from agnoclaw.runtime.operations import EffectClass
from agnoclaw.runtime.permissions import PermissionController, PermissionMode
from agnoclaw.runtime.policy import PolicyAction
from agnoclaw.runtime.security import (
    AdmissionEnvelope,
    IdentityAssertion,
    IdentitySource,
)
from agnoclaw.runtime.store import SQLiteRuntimeStore


def _context(*, scopes=("shell:execute",)) -> ExecutionContext:
    admission = AdmissionEnvelope.resolve(
        IdentityAssertion(
            source=IdentitySource.TRUSTED_HOST,
            tenant_id="tenant-1",
            user_id="user-1",
            session_id="session-1",
            scopes=scopes,
        ),
        require_trusted_tenant=True,
        require_user=True,
    )
    return ExecutionContext.create(
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="session-1",
        workspace_id="workspace-1",
        scopes=scopes,
        admission=admission,
    )


def _spec() -> CapabilitySpec:
    return CapabilitySpec(
        name="shell",
        version="1",
        kind=CapabilityKind.TOOL,
        effect_class=EffectClass.NON_REPEATABLE,
        trust=CapabilityTrust.HOST_MANAGED,
        lifetime=CapabilityLifetime.RUN,
        concurrency=CapabilityConcurrency.ISOLATED,
        recovery=CapabilityRecovery.RECONCILABLE,
        implementation_digest="sha256:shell",
        required_scopes=("shell:execute",),
    )


def _store(tmp_path) -> SQLiteRuntimeStore:
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    store.create_run(
        RunSnapshot(
            run_id="run-1",
            tenant_id="tenant-1",
            user_id="user-1",
            session_id="session-1",
        )
    )
    store.apply_transition(
        LifecycleTransition(
            run_id="run-1",
            kind=TransitionKind.START,
            transition_id="start-1",
        ),
        expected_revision=0,
    )
    return store


def _request() -> ToolCallRequest:
    return ToolCallRequest(
        run_id="run-1",
        tool_name="shell",
        arguments={"command": "true"},
        metadata={"call_id": "call-1"},
    )


async def _identity(value):
    return await value if inspect.isawaitable(value) else value


@pytest.mark.asyncio
async def test_external_host_can_settle_live_durable_wait(tmp_path) -> None:
    store = _store(tmp_path)
    context = _context()
    controller = PermissionController(
        mode=PermissionMode.DEFAULT,
        require_approver=True,
    )
    coordinator = DurableApprovalCoordinator(
        store,
        controller,
        ttl_seconds=30,
        poll_interval_seconds=0.01,
    )

    task = asyncio.create_task(
        coordinator.authorize(
            request=_request(),
            context=context,
            spec=_spec(),
            category="capability:tool",
            arguments={"command": "true"},
            policy_version="sha256:policy-v1",
            resolve_async_value=_identity,
        )
    )
    for _ in range(100):
        pending = coordinator.list_for_run(
            "run-1",
            context=context,
            states=(ApprovalState.PENDING,),
        )
        if pending:
            break
        await asyncio.sleep(0.01)
    else:  # pragma: no cover - deterministic local store
        raise AssertionError("approval request was not persisted")

    settled = coordinator.decide(
        pending[0].request.request_id,
        approved=True,
        issuer="operator-1",
        reason_code="APPROVED_BY_OPERATOR",
        context=context,
    )
    authorization = await asyncio.wait_for(task, timeout=2)

    assert settled.state is ApprovalState.APPROVED
    assert authorization.decision.action is PolicyAction.ALLOW
    assert store.get_run("run-1").state is RunState.RUNNING
    assert [event.event_type for event in store.list_events("run-1")][-4:] == [
        "run.state.changed",
        "approval.requested",
        "approval.approved",
        "run.state.changed",
    ]
    with pytest.raises(HarnessError) as drifted:
        coordinator.validate_grant(
            authorization,
            context=context,
            spec=_spec(),
            category="capability:tool",
            arguments={"command": "changed"},
            policy_version="sha256:policy-v1",
        )
    assert drifted.value.code == "AUTHORIZATION_GRANT_INVALID"
    assert drifted.value.details["field"] == "argument_digest"
    store.close()


class _Approver:
    approval_version = "test-v1"

    async def approve(self, request, context) -> bool:
        assert request.arguments == {"path": "safe.txt"}
        assert context.tenant_id == "tenant-1"
        return False


@pytest.mark.asyncio
async def test_callback_decision_is_persisted_before_returning_denial(tmp_path) -> None:
    store = _store(tmp_path)
    context = _context()
    controller = PermissionController(
        mode=PermissionMode.DEFAULT,
        approver=_Approver(),
        require_approver=True,
    )
    coordinator = DurableApprovalCoordinator(store, controller)
    request = _request()
    request.arguments = {"path": "safe.txt"}

    authorization = await coordinator.authorize(
        request=request,
        context=context,
        spec=_spec(),
        category="capability:tool",
        arguments={"path": "safe.txt"},
        policy_version="sha256:policy-v1",
        resolve_async_value=_identity,
    )

    records = coordinator.list_for_run("run-1", context=context)
    assert authorization.decision.action is PolicyAction.DENY
    assert records[0].state is ApprovalState.DENIED
    assert records[0].decision is not None
    assert records[0].decision.reason_code == "PERMISSION_REJECTED"
    assert store.get_run("run-1").state is RunState.RUNNING
    store.close()


@pytest.mark.asyncio
async def test_authority_digest_cannot_be_swapped_at_decision_time(tmp_path) -> None:
    store = _store(tmp_path)
    coordinator = DurableApprovalCoordinator(
        store,
        PermissionController(mode=PermissionMode.DEFAULT, require_approver=True),
    )
    context = _context()
    task = asyncio.create_task(
        coordinator.authorize(
            request=_request(),
            context=context,
            spec=_spec(),
            category="capability:tool",
            arguments={"command": "true"},
            policy_version="sha256:policy-v1",
            resolve_async_value=_identity,
        )
    )
    for _ in range(100):
        pending = coordinator.list_for_run(
            "run-1",
            context=context,
            states=(ApprovalState.PENDING,),
        )
        if pending:
            break
        await asyncio.sleep(0.01)
    else:  # pragma: no cover - deterministic local store
        raise AssertionError("approval request was not persisted")

    changed_authority = _context(scopes=("shell:execute", "extra"))
    with pytest.raises(HarnessError) as caught:
        coordinator.decide(
            pending[0].request.request_id,
            approved=True,
            issuer="operator-1",
            reason_code="APPROVED_BY_OPERATOR",
            context=changed_authority,
        )
    assert caught.value.code == "APPROVAL_AUTHORITY_MISMATCH"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    store.close()
