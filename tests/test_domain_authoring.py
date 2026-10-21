"""Public authoring journeys: real checks, explicit identities and reviewed dispatch."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from agnoclaw.domain import (
    Action,
    Check,
    CheckOutcome,
    CheckStatus,
    ContractRef,
    Domain,
    OutputSchema,
    OutputSpec,
    Procedure,
    ReviewVerdict,
)
from agnoclaw.runtime import EffectClass, HarnessError
from agnoclaw.runtime.security import thaw_data
from tests.test_capability_execution import _admission, _executor, _spec
from tests.test_domain_core import context, product, target


class Invoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subtotal: int
    tax: int
    total: int


def balanced(work):
    return work.content["subtotal"] + work.content["tax"] == work.content["total"]


def procedure():
    return Procedure(
        "reconcile",
        OutputSchema("invoice", Invoice),
        checks=(Check("invoice.arithmetic", balanced),),
    )


def test_output_schema_preserves_legacy_digest_and_supports_dataclass_replace():
    schema = OutputSchema("invoice", Invoice)
    assert schema.digest == OutputSpec("invoice", "1", Invoice).digest
    assert schema.ref == ContractRef.parse("invoice@1")
    assert replace(schema, version="2").ref == ContractRef("invoice", "2")


async def test_prepare_executes_bound_checks_and_rejects_invalid_inputs_first():
    flow = procedure()
    prepared = await flow.prepare(
        {"subtotal": 10, "tax": 2, "total": 12}, target=target(), run_id="run-1", context=context()
    )
    assert prepared.ready
    assert prepared.require_ready().content["total"] == 12
    assert prepared.report.results[0].reference == "invoice.arithmetic@1"
    assert prepared.report.subject_digest == prepared.product.digest
    exported = prepared.to_dict()
    exported["product"]["content"]["total"] = 100
    assert prepared.product.content["total"] == 12
    wrong = await flow.prepare(
        {"subtotal": 10, "tax": 2, "total": 99}, target=target(), run_id="run-1", context=context()
    )
    assert not wrong.ready
    with pytest.raises(HarnessError, match="required versioned check"):
        wrong.require_ready()
    with pytest.raises(ValidationError):
        await flow.prepare(
            {"subtotal": "10", "tax": 2, "total": 12},
            target=target(),
            run_id="run-1",
            context=context(),
        )
    with pytest.raises(HarnessError, match="tenant"):
        await flow.prepare(
            {"subtotal": 10, "tax": 2, "total": 12},
            target=target(),
            run_id="run-1",
            context=context("other"),
        )


async def test_check_errors_are_sanitized_unknown_is_not_success_and_cancel_propagates():
    def broken(_):
        raise ValueError("secret source text")

    for callback in (broken, lambda _: {"pass": True}, lambda _: None):
        result = await Check("broken", callback).run(product())
        assert result.status is CheckStatus.ERROR
        assert "secret" not in result.reason
    unknown = await Check("source", lambda _: CheckOutcome(CheckStatus.UNKNOWN, "No source")).run(
        product()
    )
    assert unknown.status is CheckStatus.UNKNOWN

    entered = asyncio.Event()
    stopped = asyncio.Event()

    async def wait(_):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    check = Check("wait", wait, timeout_seconds=0.01)
    assert (await check.run(product())).status is CheckStatus.ERROR
    assert stopped.is_set()
    entered.clear()
    stopped.clear()
    task = asyncio.create_task(replace(check, timeout_seconds=10).run(product()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()


def test_domain_deduplicates_identical_definitions_and_rejects_conflicts():
    flow = procedure()
    domain = Domain("accounting", (flow, flow))
    assert domain.procedures == (flow,)
    assert domain.procedure("reconcile") is flow
    assert domain.procedure(flow.ref) is flow
    assert domain.pack.procedures[0].required_checks == ("invoice.arithmetic@1",)
    assert domain.describe()["checks"][0]["reference"] == "invoice.arithmetic@1"
    with pytest.raises(HarnessError, match="Conflicting implementations"):
        Domain("bad", (flow, replace(flow, checks=(Check("invoice.arithmetic", lambda _: True),))))
    with pytest.raises(HarnessError, match="Conflicting definitions"):
        Domain("bad", (flow, replace(flow, skill="other-skill")))
    with pytest.raises(HarnessError, match="Conflicting output"):
        Domain(
            "bad",
            (
                flow,
                replace(
                    flow,
                    name="other",
                    output=OutputSchema("invoice", Invoice, metadata={"different": True}),
                ),
            ),
        )
    multi = Domain("versions", (flow, replace(flow, version="2")))
    with pytest.raises(HarnessError, match="Multiple versions"):
        multi.procedure("reconcile")
    assert multi.procedure("reconcile@2").version == "2"
    with pytest.raises(HarnessError, match="No procedure"):
        domain.procedure("missing")


@pytest.mark.parametrize("reference", ["invoice", "invoice@", "@1", "invoice@1@2", "bad name@1"])
def test_reference_parser_rejects_ambiguous_or_unversioned_names(reference):
    with pytest.raises(ValueError):
        ContractRef.parse(reference)


async def test_check_version_change_prevents_old_report_from_completing_new_procedure():
    flow = procedure()
    prepared = await flow.prepare(
        {"subtotal": 10, "tax": 2, "total": 12}, target=target(), run_id="run-1", context=context()
    )
    newer = replace(flow, checks=(replace(flow.checks[0], version="2"),))
    with pytest.raises(HarnessError, match="required versioned check"):
        newer.spec.verify_completion(prepared.product, prepared.report)
    assert newer.digest != flow.digest


async def test_reviewed_dispatch_uses_exact_arguments_and_runtime_replay(tmp_path):
    calls = []
    spec = _spec(
        effect=EffectClass.IDEMPOTENT, factory=lambda: lambda value: calls.append(value) or value
    )
    action = Action(spec, checks=(Check("value.positive", lambda p: p.arguments["value"] > 0),))
    prepared = await action.prepare(product(), {"value": 120}, context=context())
    now = datetime.now(UTC)
    reviewer = replace(context(), scopes=("review",))
    review = prepared.request_review(
        summary="Apply revenue", required_scope="review", expires_at=now + timedelta(hours=1)
    ).decide(ReviewVerdict.APPROVE, context=reviewer, now=now)
    executor, store, _ = _executor(tmp_path, spec)
    identity = prepared.operation_identity("request-1")
    loaded = []

    async def load_review(value):
        loaded.append(value.review_key)
        return review

    async def load_current(object_id):
        assert object_id == target().identity
        return target()

    kwargs = dict(
        run_id="run-1",
        attempt_id="attempt-1",
        operation_id="apply",
        admission=_admission(),
        load_current=load_current,
        load_review=load_review,
        idempotency_key=identity.key,
        clock=lambda: now,
    )
    try:
        first = await action.execute(executor, prepared, **kwargs)
        replay = await action.execute(executor, prepared, **kwargs)
        assert first.value == 120
        assert replay.replayed
        assert calls == [120]
        assert loaded and set(loaded) == {prepared.review_key}
    finally:
        await executor.aclose()
        store.close()


@pytest.mark.parametrize(
    "failure",
    [
        "missing_review",
        "stale",
        "wrong_tenant",
        "unknown_check",
        "policy_changed",
        "implementation_changed",
    ],
)
async def test_reviewed_dispatch_rejects_before_side_effects(tmp_path, failure):
    writes = []
    spec = _spec(factory=lambda: lambda value: writes.append(value))
    action = Action(spec, checks=(Check("valid", lambda _: True),))
    prepared = await action.prepare(product(), {"value": 120}, context=context())
    now = datetime.now(UTC)
    approved = prepared.request_review(
        summary="Apply", required_scope="review", expires_at=now + timedelta(hours=1)
    ).decide(ReviewVerdict.APPROVE, context=replace(context(), scopes=("review",)), now=now)
    current = target()
    admission = _admission()
    registered = spec
    if failure == "missing_review":
        approved = None
    elif failure == "stale":
        current = target("2")
    elif failure == "wrong_tenant":
        admission = _admission(tenant_id="other")
    elif failure == "unknown_check":
        action = Action(
            spec,
            checks=(Check("valid", lambda _: CheckOutcome(CheckStatus.UNKNOWN, "Missing source")),),
        )
        prepared = await action.prepare(product(), {"value": 120}, context=context())
    elif failure == "policy_changed":
        action = replace(action, checks=(replace(action.checks[0], version="2"),))
    else:
        registered = replace(
            spec,
            implementation_digest="sha256:changed-implementation",
            input_schema=thaw_data(spec.input_schema),
        )
    executor, store, _ = _executor(tmp_path, registered)
    try:
        with pytest.raises(HarnessError):
            await action.execute(
                executor,
                prepared,
                run_id="run-1",
                attempt_id="attempt-1",
                operation_id="apply",
                admission=admission,
                load_current=lambda _: current,
                load_review=lambda _: approved,
                clock=lambda: now,
            )
        assert not writes
    finally:
        await executor.aclose()
        store.close()
