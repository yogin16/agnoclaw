"""Declared child template contracts for model-visible delegation."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from agno.run.agent import RunOutput
from agno.tools.function import Function, FunctionCall

from agnoclaw import AgentHarness, DeclaredChildTemplate, HarnessConfig
from agnoclaw.capability_governance import capability_digest
from agnoclaw.runtime import (
    ChildJoinPolicy,
    ChildRunBudget,
    ChildRunContractError,
    ExecutionContext,
    LocalArtifactStore,
    SQLiteRuntimeStore,
)

RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "minLength": 1, "maxLength": 100},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["answer", "confidence"],
    "additionalProperties": False,
}


class StructuredChildAgent:
    def __init__(self, **kwargs):
        self.system_message = kwargs["system_message"]
        self.session_id = kwargs.get("session_id")
        self.user_id = kwargs.get("user_id")
        self.tools = list(kwargs.get("tools") or [])

    async def arun(self, message, **_kwargs):
        return RunOutput(content={"answer": f"checked:{message}", "confidence": 0.9})


class LargeChildAgent(StructuredChildAgent):
    async def arun(self, _message, **_kwargs):
        return RunOutput(content="x" * 2_000)


class DelegatingParentAgent:
    task = "inspect evidence"
    delegation_id = "research-1"
    call_id = "call-delegate-1"

    def __init__(self, **kwargs):
        self.system_message = kwargs["system_message"]
        self.session_id = kwargs.get("session_id")
        self.user_id = kwargs.get("user_id")
        self.tools = list(kwargs.get("tools") or [])

    async def arun(self, _message, **kwargs):
        function = next(tool for tool in self.tools if isinstance(tool, Function))
        call = FunctionCall(
            function=function,
            arguments={"task": self.task, "delegation_id": self.delegation_id},
            call_id=self.call_id,
        )
        run_context = SimpleNamespace(
            run_id=kwargs["run_id"],
            metadata=kwargs["metadata"],
            user_id=kwargs.get("user_id"),
            session_id=kwargs.get("session_id"),
        )
        assert function.pre_hook is not None
        function.pre_hook(agent=self, run_context=run_context, fc=call)
        try:
            call.result = await function.entrypoint(**dict(call.arguments or {}))
            return RunOutput(content=call.result)
        except Exception as exc:
            call.error = str(exc)
            raise
        finally:
            assert function.post_hook is not None
            function.post_hook(agent=self, run_context=run_context, fc=call)


class LargeDelegatingParentAgent(DelegatingParentAgent):
    task = "large"
    delegation_id = "large-1"
    call_id = "call-large-1"


def _harness(agent_type, tmp_path, store, artifacts, **kwargs):
    with patch("agnoclaw.agent.Agent", agent_type):
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            return AgentHarness(
                model="model",
                provider="custom",
                config=HarnessConfig(enable_plugins=False),
                workspace_dir=tmp_path / agent_type.__name__,
                include_default_tools=False,
                runtime_store=store,
                artifact_store=artifacts,
                tenant_id="tenant-1",
                user_id="user-1",
                **kwargs,
            )


@pytest.mark.asyncio
async def test_model_visible_template_dispatches_normal_declared_child(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    child = _harness(StructuredChildAgent, tmp_path, store, artifacts)
    template = DeclaredChildTemplate(
        name="delegate_research",
        child_harness=child,
        purpose_code="research",
        budget=ChildRunBudget(max_depth=2, max_fanout=2, max_tokens=1_000),
        join_policy=ChildJoinPolicy.ALL_SUCCESS,
        result_schema=RESULT_SCHEMA,
        persist_output=True,
    )
    parent = _harness(
        DelegatingParentAgent,
        tmp_path,
        store,
        artifacts,
        capabilities=[template],
        max_inline_output_chars=4_096,
    )
    context = ExecutionContext.create(
        tenant_id="tenant-1",
        user_id="user-1",
        session_id="parent-session",
        workspace_id="workspace",
    )

    run = await parent.start("delegate", context=context)
    result = await run.wait()

    assert result.content["delegation_id"] == "research-1"
    assert result.content["succeeded"] is True
    assert result.content["result"]["content"]["answer"] == "checked:inspect evidence"
    children = await run.children()
    assert len(children) == 1
    spec = store.get_child_spec(children[0].run_id)
    assert spec.parent_tool_call_id == "call-delegate-1"
    assert spec.parent_step_id is not None
    operation_id = (
        f"{run.run_id}:capability:"
        f"{capability_digest(['delegate_research@1.0.0', 'call-delegate-1']).split(':', 1)[1][:32]}"
    )
    operation = store.get_operation(operation_id)
    assert operation.intent.idempotency_key == "research-1"
    await parent.aclose()
    await child.aclose()


@pytest.mark.asyncio
async def test_model_visible_template_returns_lossless_pointer_instead_of_truncation(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    child = _harness(LargeChildAgent, tmp_path, store, artifacts)
    template = DeclaredChildTemplate(
        name="delegate_large",
        child_harness=child,
        purpose_code="research",
        max_inline_result_chars=256,
    )
    parent = _harness(
        LargeDelegatingParentAgent,
        tmp_path,
        store,
        artifacts,
        capabilities=[template],
        max_inline_output_chars=4_096,
    )
    run = await parent.start(
        "delegate",
        context=ExecutionContext.create(
            tenant_id="tenant-1",
            user_id="user-1",
            session_id="parent-session",
            workspace_id="workspace",
        ),
    )
    result = await run.wait()

    pointer = result.content["result"]
    assert pointer["type"] == "agnoclaw.child_result_artifact"
    assert pointer["inline"] is False
    assert pointer["rendered_chars"] > 2_000
    assert pointer["artifact"]["artifact_id"].startswith("artifact:")
    assert "x" * 300 not in str(result.content)
    await parent.aclose()
    await child.aclose()


@pytest.mark.asyncio
async def test_template_manifest_is_policy_bound_and_rejects_reentrant_harness(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.db")
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    child = _harness(StructuredChildAgent, tmp_path, store, artifacts)
    first = DeclaredChildTemplate(
        name="delegate_research",
        child_harness=child,
        purpose_code="research",
        learning_allowed=False,
    )
    second = DeclaredChildTemplate(
        name="delegate_research",
        child_harness=child,
        purpose_code="research",
        learning_allowed=True,
    )

    assert first.digest != second.digest
    with pytest.raises(ChildRunContractError) as reentrant:
        first.capability(child)
    assert reentrant.value.code == "CHILD_HARNESS_REENTRANT"
    await child.aclose()
    store.close()
