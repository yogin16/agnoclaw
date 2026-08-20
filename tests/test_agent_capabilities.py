"""AgentHarness-owned capability ingress, governance, and lease contracts."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from agno.tools.function import Function, FunctionCall

from agnoclaw import (
    AgentHarness,
    CapabilityConcurrency,
    CapabilityKind,
    CapabilityLifetime,
    CapabilityRecovery,
    CapabilitySpec,
    CapabilityTrust,
    EffectClass,
    HarnessConfig,
)
from agnoclaw.output_spill import read_capability
from agnoclaw.runtime import (
    ApprovalState,
    HarnessError,
    InMemoryEventSink,
    LocalArtifactStore,
    PolicyAction,
    PolicyDecision,
    RedactionRule,
    RunReconciliationRequiredError,
    RuntimeLeaseLostError,
    SQLiteRuntimeStore,
)


class ToolCallingAgent:
    repeat = 1
    arguments = {"idempotency_key": "provider-key-1", "value": "input"}

    def __init__(self, **kwargs):
        self.system_message = kwargs["system_message"]
        self.session_id = kwargs.get("session_id")
        self.user_id = kwargs.get("user_id")
        self.tools = list(kwargs.get("tools") or [])
        self.db = kwargs.get("db")
        self.learning = kwargs.get("learning")

    async def arun(self, _message, **kwargs):
        function = next(tool for tool in self.tools if isinstance(tool, Function))
        result = None
        for _ in range(self.repeat):
            call = FunctionCall(
                function=function,
                arguments=dict(self.arguments),
                call_id="call-capability-1",
            )
            run_context = SimpleNamespace(
                run_id=kwargs["run_id"],
                metadata=kwargs["metadata"],
                user_id=kwargs.get("user_id"),
                session_id=kwargs.get("session_id"),
            )
            if function.pre_hook is not None:
                function.pre_hook(agent=self, run_context=run_context, fc=call)
            try:
                result = await function.entrypoint(**dict(call.arguments or {}))
                call.result = result
            except Exception as exc:
                call.error = str(exc)
                raise
            finally:
                if function.post_hook is not None:
                    function.post_hook(agent=self, run_context=run_context, fc=call)
        return SimpleNamespace(content=result)


class SpillReadingAgent(ToolCallingAgent):
    @staticmethod
    async def _invoke(function, *, kwargs, arguments, call_id):
        call = FunctionCall(function=function, arguments=arguments, call_id=call_id)
        run_context = SimpleNamespace(
            run_id=kwargs["run_id"],
            metadata=kwargs["metadata"],
            user_id=kwargs.get("user_id"),
            session_id=kwargs.get("session_id"),
        )
        if function.pre_hook is not None:
            function.pre_hook(agent=None, run_context=run_context, fc=call)
        try:
            result = await function.entrypoint(**dict(call.arguments or {}))
            call.result = result
            return result
        except Exception as exc:
            call.error = str(exc)
            raise
        finally:
            if function.post_hook is not None:
                function.post_hook(agent=None, run_context=run_context, fc=call)

    async def arun(self, _message, **kwargs):
        reader = next(
            tool
            for tool in self.tools
            if isinstance(tool, Function) and tool.name == "read_spilled_output"
        )
        producer = next(
            tool
            for tool in self.tools
            if isinstance(tool, Function) and tool.name != "read_spilled_output"
        )
        spill = await self._invoke(
            producer,
            kwargs=kwargs,
            arguments=dict(self.arguments),
            call_id="call-spill-producer",
        )
        page = await self._invoke(
            reader,
            kwargs=kwargs,
            arguments={"artifact_id": spill["id"], "offset": 0, "limit": 10_000},
            call_id="call-spill-reader",
        )
        return SimpleNamespace(content={"spill": spill, "page": page})


class CrossRunReadingAgent(SpillReadingAgent):
    prior_artifact_id = None

    async def arun(self, _message, **kwargs):
        reader = next(
            tool
            for tool in self.tools
            if isinstance(tool, Function) and tool.name == "read_spilled_output"
        )
        if self.prior_artifact_id is not None:
            return SimpleNamespace(
                content=await self._invoke(
                    reader,
                    kwargs=kwargs,
                    arguments={"artifact_id": self.prior_artifact_id},
                    call_id="call-cross-run-reader",
                )
            )
        producer = next(
            tool
            for tool in self.tools
            if isinstance(tool, Function) and tool.name != "read_spilled_output"
        )
        return SimpleNamespace(
            content=await self._invoke(
                producer,
                kwargs=kwargs,
                arguments=dict(self.arguments),
                call_id="call-cross-run-producer",
            )
        )


class AsyncApprover:
    approval_version = "approval-policy-3"
    calls = 0

    async def approve(self, request, context):
        self.calls += 1
        assert request.category == "capability:tool"
        assert context.tenant_id == "tenant-1"
        return True


class RedactingPolicy:
    policy_version = "policy-7"

    def before_run(self, _run_input, _context):
        return PolicyDecision.allow()

    def before_prompt_send(self, _prompt, _context):
        return PolicyDecision.allow()

    def before_skill_load(self, _request, _context):
        return PolicyDecision.allow()

    async def before_tool_call(self, request, _context):
        assert request.metadata["capability_kind"] == "tool"
        return PolicyDecision(
            action=PolicyAction.ALLOW_WITH_CONSTRAINTS,
            reason_code="CAPABILITY_CONSTRAINED",
            constraints={
                "arguments": {"policy_bound": True},
                "max_timeout_seconds": 5,
                "require_idempotency_key": True,
            },
        )

    async def after_tool_call(self, result, _context):
        assert result.metadata["effect_class"] == "idempotent"
        return PolicyDecision(
            action=PolicyAction.ALLOW_WITH_REDACTION,
            reason_code="CAPABILITY_OUTPUT_REDACTED",
            redactions=(RedactionRule(target="secret", replacement="[MASKED]"),),
        )


class UnsafeConstraintPolicy(RedactingPolicy):
    policy_version = "unsafe-constraint-policy-1"

    async def before_tool_call(self, _request, _context):
        return PolicyDecision(
            action=PolicyAction.ALLOW_WITH_CONSTRAINTS,
            reason_code="FORCE_UNSAFE_PATH",
            constraints={"arguments": {"path": "/outside-workspace/secret"}},
        )


def _capability(factory) -> CapabilitySpec:
    return CapabilitySpec(
        name="inventory.mutate",
        version="1.2.0",
        kind=CapabilityKind.TOOL,
        effect_class=EffectClass.IDEMPOTENT,
        trust=CapabilityTrust.VERIFIED,
        lifetime=CapabilityLifetime.RUN,
        concurrency=CapabilityConcurrency.ISOLATED,
        recovery=CapabilityRecovery.RECREATABLE,
        implementation_digest="sha256:inventory-mutate-v1.2.0",
        description="Update one inventory record with provider deduplication.",
        input_schema={
            "type": "object",
            "properties": {
                "idempotency_key": {"type": "string"},
                "value": {"type": "string"},
            },
            "required": ["idempotency_key", "value"],
        },
        supports_idempotency_key=True,
        factory=factory,
    )


def _harness(
    tmp_path,
    capability,
    *,
    store=None,
    artifacts=None,
    agent_type=ToolCallingAgent,
    session_id="session-1",
    **kwargs,
):
    runtime_store = store or SQLiteRuntimeStore(tmp_path / "runtime.db")
    with patch("agnoclaw.agent.Agent", agent_type):
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            harness = AgentHarness(
                model="model",
                provider="custom",
                workspace_dir=tmp_path / "workspace",
                config=HarnessConfig(enable_plugins=False),
                include_default_tools=False,
                capabilities=[capability],
                runtime_store=runtime_store,
                artifact_store=artifacts,
                tenant_id="tenant-1",
                user_id="user-1",
                session_id=session_id,
                scopes=["inventory:write"],
                **kwargs,
            )
    return harness, runtime_store


@pytest.mark.asyncio
async def test_large_capability_output_spills_and_pages_inside_owning_run(tmp_path):
    output = "A" * 5_000

    def factory():
        async def invoke(**_arguments):
            return output

        return invoke

    ToolCallingAgent.repeat = 1
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    events = InMemoryEventSink()
    harness, store = _harness(
        tmp_path,
        _capability(factory),
        artifacts=artifacts,
        agent_type=SpillReadingAgent,
        event_sink=events,
        max_inline_output_chars=1_024,
        permission_mode="bypass",
    )

    run = await harness.start("produce and inspect a large result")
    result = await run.wait()
    spill = result.content["spill"]
    page = result.content["page"]

    assert spill["type"] == "agnoclaw.spilled_output"
    assert spill["rendered_chars"] == 5_000
    assert output not in str(spill)
    assert page["type"] == "agnoclaw.spilled_output_page"
    assert page["content"] == "A" * 512
    assert page["next_offset"] == 512
    assert page["complete"] is False
    reference = store.get_artifact(spill["id"])
    assert reference.scope.run_id == str(run.run_id)
    assert await artifacts.load_json(reference) == output
    spill_events = [event for event in events.events if event.event_type == "output.spilled"]
    assert len(spill_events) == 1
    assert spill_events[0].payload["artifact_id"] == spill["id"]
    assert spill_events[0].payload["rendered_chars"] == 5_000
    await harness.aclose()
    store.close()


def test_output_spill_requires_storage_and_reserves_its_reader_name(tmp_path):
    runtime_store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    with pytest.raises(HarnessError) as missing_store:
        _harness(
            tmp_path,
            _capability(lambda: lambda **_arguments: "result"),
            store=runtime_store,
            max_inline_output_chars=1_024,
        )
    assert missing_store.value.code == "OUTPUT_SPILL_ARTIFACT_STORE_REQUIRED"

    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    with pytest.raises(HarnessError) as conflict:
        _harness(
            tmp_path,
            read_capability(lambda: object()),
            store=runtime_store,
            artifacts=artifacts,
            max_inline_output_chars=1_024,
        )
    assert conflict.value.code == "OUTPUT_SPILL_TOOL_CONFLICT"
    runtime_store.close()


@pytest.mark.parametrize("limit", [True, 1_023, 1_000_001])
def test_direct_output_spill_limit_fails_at_construction(tmp_path, limit):
    runtime_store = SQLiteRuntimeStore(tmp_path / f"runtime-{limit}.db")
    artifacts = LocalArtifactStore(tmp_path / f"artifacts-{limit}")

    with pytest.raises(HarnessError) as caught:
        _harness(
            tmp_path,
            _capability(lambda: lambda **_arguments: "result"),
            store=runtime_store,
            artifacts=artifacts,
            max_inline_output_chars=limit,
        )

    assert caught.value.code == "OUTPUT_SPILL_LIMIT_INVALID"
    runtime_store.close()


@pytest.mark.asyncio
async def test_spilled_output_survives_same_session_but_not_cross_session(tmp_path):
    def factory():
        async def invoke(**_arguments):
            return "cross-run-secret" * 400

        return invoke

    CrossRunReadingAgent.prior_artifact_id = None
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    harness, store = _harness(
        tmp_path,
        _capability(factory),
        artifacts=artifacts,
        agent_type=CrossRunReadingAgent,
        max_inline_output_chars=1_024,
        permission_mode="bypass",
    )

    first = await harness.start("produce output")
    first_result = await first.wait()
    CrossRunReadingAgent.prior_artifact_id = first_result.content["id"]
    second = await harness.start("continue from stored output")
    second_result = await second.wait()

    assert second_result.content["content"] == ("cross-run-secret" * 400)[:512]
    assert second_result.content["artifact_id"] == CrossRunReadingAgent.prior_artifact_id

    await harness.aclose()
    other_harness, _ = _harness(
        tmp_path,
        _capability(factory),
        store=store,
        artifacts=artifacts,
        agent_type=CrossRunReadingAgent,
        session_id="session-2",
        max_inline_output_chars=1_024,
        permission_mode="bypass",
    )
    third = await other_harness.start("try output from another session")

    with pytest.raises(RunReconciliationRequiredError):
        await third.wait()
    assert store.get_terminal(str(third.run_id)) is None
    operation = store.get_operation(f"{third.run_id}:model:1")
    assert operation.settlement.safe_error["code"] == "OUTPUT_SPILL_SCOPE_MISMATCH"
    await other_harness.aclose()
    store.close()
    CrossRunReadingAgent.prior_artifact_id = None


@pytest.mark.asyncio
async def test_agent_capability_ingress_is_async_governed_fenced_and_replayed(tmp_path):
    calls = 0
    received = []

    def factory():
        async def invoke(**arguments):
            nonlocal calls
            calls += 1
            received.append(arguments)
            return {"value": "secret-result"}

        return invoke

    ToolCallingAgent.repeat = 2
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    approver = AsyncApprover()
    harness, store = _harness(
        tmp_path,
        _capability(factory),
        artifacts=artifacts,
        policy_engine=RedactingPolicy(),
        permission_mode="default",
        permission_approver=approver,
        permission_require_approver=True,
    )

    run = await harness.start("update inventory")
    result = await run.wait()

    assert result.content == {"value": "[MASKED]-result"}
    assert calls == 1
    assert approver.calls == 1
    assert received == [
        {
            "idempotency_key": "provider-key-1",
            "value": "input",
            "policy_bound": True,
        }
    ]
    planned = [
        event
        for event in store.list_events(str(run.run_id))
        if event.event_type == "operation.planned"
        and ":capability:" in event.payload["operation_id"]
    ]
    assert len(planned) == 1
    operation = store.get_operation(planned[0].payload["operation_id"])
    assert operation.settlement is not None
    assert operation.intent.idempotency_key == "provider-key-1"
    assert operation.intent.metadata["lease_evidence"]["run_fence_token"] == 1
    assert [item["checkpoint"] for item in operation.intent.metadata["policy_evidence"]] == [
        "permission.before_tool_call",
        "before_tool_call",
    ]
    persisted = str(operation.to_dict())
    assert "secret-result" not in persisted
    reference = store.get_artifact(operation.settlement.result_reference)
    assert await artifacts.load_json(reference) == {"value": "[MASKED]-result"}
    approvals = store.list_approvals(str(run.run_id))
    assert len(approvals) == 1
    assert all(record.state is ApprovalState.APPROVED for record in approvals)
    assert all(record.grant is not None for record in approvals)
    assert all(
        record.request.argument_digest == record.grant.argument_digest  # type: ignore[union-attr]
        for record in approvals
    )
    await harness.aclose()
    store.close()


@pytest.mark.asyncio
async def test_host_api_can_approve_a_waiting_agent_capability(tmp_path):
    calls = 0

    def factory():
        async def invoke(**_arguments):
            nonlocal calls
            calls += 1
            return {"approved": True}

        return invoke

    ToolCallingAgent.repeat = 1
    harness, store = _harness(
        tmp_path,
        _capability(factory),
        permission_mode="default",
        permission_require_approver=True,
    )

    run = await harness.start("update inventory")
    for _ in range(100):
        pending = harness.capability_approvals(
            str(run.run_id),
            states=(ApprovalState.PENDING,),
        )
        if pending:
            break
        await asyncio.sleep(0.01)
    else:  # pragma: no cover - deterministic local ledger
        raise AssertionError("agent did not enter durable approval wait")

    settled = harness.decide_capability_approval(
        pending[0].request.request_id,
        run_id=str(run.run_id),
        approved=True,
        issuer="operator-1",
        reason_code="APPROVED_BY_OPERATOR",
    )
    replayed = harness.decide_capability_approval(
        pending[0].request.request_id,
        run_id=str(run.run_id),
        approved=True,
        issuer="operator-1",
        reason_code="APPROVED_BY_OPERATOR",
    )
    with pytest.raises(HarnessError) as conflicting:
        harness.decide_capability_approval(
            pending[0].request.request_id,
            run_id=str(run.run_id),
            approved=False,
            issuer="operator-1",
            reason_code="REJECTED_BY_OPERATOR",
        )
    result = await run.wait()

    assert settled.state is ApprovalState.APPROVED
    assert replayed == settled
    assert conflicting.value.code == "APPROVAL_ALREADY_SETTLED"
    assert result.content == {"approved": True}
    assert calls == 1
    assert store.get_run(str(run.run_id)).state.value == "completed"
    assert "approval.requested" in [
        event.event_type for event in store.list_events(str(run.run_id))
    ]
    assert [
        event.event_type for event in store.list_events(str(run.run_id))
    ].count("approval.approved") == 1
    await harness.aclose()
    store.close()


@pytest.mark.asyncio
async def test_approved_grant_is_rechecked_for_policy_drift_before_dispatch(tmp_path):
    factories = 0

    def factory():
        nonlocal factories
        factories += 1
        return lambda **_arguments: "unexpected"

    class DriftApprover:
        approval_version = "drift-test-v1"
        harness = None

        async def approve(self, _request, _context):
            self.harness._permission_controller.set_mode("bypass")
            return True

    approver = DriftApprover()
    ToolCallingAgent.repeat = 1
    harness, store = _harness(
        tmp_path,
        _capability(factory),
        permission_mode="default",
        permission_approver=approver,
        permission_require_approver=True,
    )
    approver.harness = harness

    run = await harness.start("update inventory")
    with pytest.raises(RunReconciliationRequiredError):
        await run.wait()

    assert factories == 0
    assert store.get_terminal(str(run.run_id)) is None
    operation = store.get_operation(f"{run.run_id}:model:1")
    assert operation.settlement.safe_error["code"] == "AUTHORIZATION_GRANT_INVALID"
    await harness.aclose()
    store.close()


@pytest.mark.asyncio
async def test_cancelling_live_approval_wait_tombstones_request(tmp_path):
    factories = 0

    def factory():
        nonlocal factories
        factories += 1
        return lambda **_arguments: "unexpected"

    ToolCallingAgent.repeat = 1
    harness, store = _harness(
        tmp_path,
        _capability(factory),
        permission_mode="default",
        permission_require_approver=True,
    )
    run = await harness.start("update inventory")
    for _ in range(100):
        pending = harness.capability_approvals(
            str(run.run_id),
            states=(ApprovalState.PENDING,),
        )
        if pending:
            break
        await asyncio.sleep(0.01)
    else:  # pragma: no cover - deterministic local ledger
        raise AssertionError("agent did not enter durable approval wait")

    cancelled = await run.cancel()

    assert cancelled.state.value == "waiting_for_reconciliation"
    assert factories == 0
    record = store.get_approval(pending[0].request.request_id)
    assert record.state is ApprovalState.CANCELLED
    assert "approval.cancelled" in [
        event.event_type for event in store.list_events(str(run.run_id))
    ]
    await harness.aclose()
    store.close()


@pytest.mark.asyncio
async def test_capability_cannot_execute_outside_active_run_or_materialize(tmp_path):
    factories = 0

    def factory():
        nonlocal factories
        factories += 1
        return lambda **_arguments: "unexpected"

    ToolCallingAgent.repeat = 1
    harness, store = _harness(tmp_path, _capability(factory))

    with pytest.raises(HarnessError) as failure:
        await harness.aexecute_capability(
            "inventory.mutate",
            operation_id="outside-run",
            arguments={"idempotency_key": "key"},
        )

    assert failure.value.code == "CAPABILITY_ACTIVE_RUN_REQUIRED"
    assert factories == 0

    with pytest.raises(HarnessError) as direct:
        await harness.arun("try the capability")
    assert direct.value.code == "CAPABILITY_ACTIVE_RUN_REQUIRED"
    assert factories == 0
    await harness.aclose()
    store.close()


@pytest.mark.asyncio
async def test_lost_lease_fails_capability_before_external_dispatch(tmp_path):
    factories = 0

    def factory():
        nonlocal factories
        factories += 1
        return lambda **_arguments: "unexpected"

    store = SQLiteRuntimeStore(tmp_path / "runtime.db")

    def lose_lease(claim, *, lease_seconds=30):
        del lease_seconds
        raise RuntimeLeaseLostError(run_id=claim.run_id, kind=claim.run.kind)

    store.renew_run_lease = lose_lease  # type: ignore[method-assign]
    ToolCallingAgent.repeat = 1
    harness, _ = _harness(
        tmp_path,
        _capability(factory),
        store=store,
        policy_engine=RedactingPolicy(),
        permission_mode="bypass",
    )

    run = await harness.start("update inventory")
    with pytest.raises(RunReconciliationRequiredError):
        await run.wait()

    capability_operations = [
        event.payload["operation_id"]
        for event in store.list_events(str(run.run_id))
        if event.event_type == "operation.planned"
        and ":capability:" in event.payload["operation_id"]
    ]
    assert len(capability_operations) == 1
    operation = store.get_operation(capability_operations[0])
    assert operation.state.value == "failed"
    assert factories == 0
    await harness.aclose()
    store.close()


@pytest.mark.asyncio
async def test_durable_capability_requires_versioned_custom_policy(tmp_path):
    factories = 0

    def factory():
        nonlocal factories
        factories += 1
        return lambda **_arguments: "unexpected"

    class UnversionedPolicy(RedactingPolicy):
        policy_version = None

    ToolCallingAgent.repeat = 1
    harness, store = _harness(
        tmp_path,
        _capability(factory),
        policy_engine=UnversionedPolicy(),
        permission_mode="bypass",
    )

    run = await harness.start("update inventory")
    with pytest.raises(RunReconciliationRequiredError):
        await run.wait()
    assert factories == 0
    assert store.get_terminal(str(run.run_id)) is None
    operation = store.get_operation(f"{run.run_id}:model:1")
    assert operation.settlement.safe_error["code"] == "CAPABILITY_POLICY_VERSION_REQUIRED"
    await harness.aclose()
    store.close()


@pytest.mark.asyncio
async def test_policy_constrained_arguments_are_rechecked_by_guardrails(tmp_path):
    factories = 0

    def factory():
        nonlocal factories
        factories += 1
        return lambda **_arguments: "unexpected"

    ToolCallingAgent.repeat = 1
    harness, store = _harness(
        tmp_path,
        _capability(factory),
        policy_engine=UnsafeConstraintPolicy(),
        permission_mode="bypass",
    )

    run = await harness.start("update inventory")
    with pytest.raises(RunReconciliationRequiredError):
        await run.wait()

    assert factories == 0
    assert store.get_terminal(str(run.run_id)) is None
    operation = store.get_operation(f"{run.run_id}:model:1")
    assert operation.settlement.safe_error["code"] == "GUARDRAIL_DENIED"
    await harness.aclose()
    store.close()
