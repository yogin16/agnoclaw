"""Domain contracts reject stale, unscoped and unverifiable work before side effects."""

import asyncio
import inspect
import threading
from dataclasses import replace

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from agnoclaw.capability_schema import validate_capability_arguments
from agnoclaw.domain import (
    CheckResult,
    CheckStatus,
    ObjectRef,
    ObjectSnapshot,
    OutputSpec,
    TextSource,
    VerificationReport,
    bind_output_capability,
    bind_output_schema,
    check_quote,
    locate_quote,
    run_check,
)
from agnoclaw.runtime import HarnessError
from agnoclaw.runtime.context import ExecutionContext
from agnoclaw.runtime.security import canonical_json_digest
from tests.test_capability_execution import _admission, _executor, _spec


class Amount(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: int
    currency: str


class Extraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revenue: Amount


def context(tenant="tenant-1"):
    return ExecutionContext.create(
        tenant_id=tenant,
        user_id="user-1",
        session_id="session-1",
        workspace_id=None,
    )


def target(revision="1"):
    return ObjectRef("tenant-1", "deal", "deal-1", revision)


def source(text="Revenue: USD 120"):
    return TextSource(ObjectRef("tenant-1", "document", "deck-1", "v1"), text)


def output():
    return OutputSpec("deal-extraction", "1", Extraction, {"artifact_type": "field_extraction"})


def product():
    return output().build(
        {"revenue": {"value": 120, "currency": "USD"}},
        target=target(),
        context=context(),
        run_id="run-1",
    )


def test_snapshot_and_product_are_deeply_immutable_and_json_export_is_detached():
    data = {"nested": [1]}
    snapshot = ObjectSnapshot(target(), data)
    data["nested"].append(2)
    assert snapshot.data["nested"] == (1,)
    value = product()
    before = value.digest
    exported = value.to_dict()
    exported["content"]["revenue"]["value"] = 999
    assert value.digest == before
    with pytest.raises(TypeError):
        value.content["revenue"]["value"] = 999
    with pytest.raises(TypeError):
        ObjectSnapshot(target(), {"value": float("nan")})


@pytest.mark.parametrize("quote", ["Net profit: 25", "Net  proﬁt:\n25"])
def test_ocr_whitespace_and_ligatures_preserve_original_offsets(quote):
    text = source("Report\nNet\tproﬁt:  25\nEnd")
    match = locate_quote(text, quote)
    assert match.status == "matched"
    evidence = match.evidence
    assert evidence is not None
    assert text.text[evidence.start : evidence.end] == "Net\tproﬁt:  25"
    assert check_quote(evidence, text).status is CheckStatus.PASS
    assert "semantic support not assessed" in check_quote(evidence, text).reason
    assert (
        check_quote(evidence, replace(text, text=text.text + "changed")).status is CheckStatus.FAIL
    )
    assert check_quote(evidence, replace(text, extractor_version="v2")).status is CheckStatus.FAIL


def test_quote_matching_abstains_on_duplicates_and_never_fuzzy_matches_amounts():
    assert locate_quote(source("ﬁ"), "f").status == "missing"
    assert locate_quote(source("120 then 120"), "120").status == "ambiguous"
    assert locate_quote(source("Revenue: 120"), "Revenue: 12O").status == "missing"
    assert locate_quote(source("Revenue: 120"), "Revenue: 121").status == "missing"
    with pytest.raises(ValueError):
        locate_quote(source(), "  ")
    match = locate_quote(source(), "Revenue: USD 120")
    assert match.evidence is not None
    other_tenant = replace(source().reference, tenant_id="tenant-2")
    assert check_quote(match.evidence, replace(source(), reference=other_tenant)).status == "fail"


def test_output_rejects_foreign_scope_and_untrusted_metadata_and_coercion():
    spec = output()
    payload = {"revenue": {"value": 120, "currency": "USD"}}
    with pytest.raises(HarnessError, match="tenant"):
        spec.build(payload, target=target(), context=context("tenant-2"), run_id="run-1")
    with pytest.raises(ValidationError):
        spec.build(
            {**payload, "artifact_type": "deal_memo"},
            target=target(),
            context=context(),
            run_id="run-1",
        )
    with pytest.raises(ValidationError):
        spec.build(
            {"revenue": {"value": "120", "currency": "USD"}},
            target=target(),
            context=context(),
            run_id="run-1",
        )
    evidence = locate_quote(source(), "120").evidence
    assert evidence is not None
    foreign = replace(evidence, source=replace(evidence.source, tenant_id="tenant-2"))
    with pytest.raises(HarnessError):
        spec.build(payload, target=target(), context=context(), run_id="run-1", evidence=[foreign])


def test_output_schema_binding_preserves_host_arguments_and_definition_identity():
    base = {
        "type": "object",
        "properties": {"content": {}, "title": {"$ref": "#/$defs/Amount"}},
        "$defs": {"Amount": {"type": "string"}},
        "required": ["title"],
        "additionalProperties": False,
    }
    bound = bind_output_schema(base, output())
    assert bound["$defs"]["Amount"] == {"type": "string"}
    content = bound["properties"]["content"]
    assert content["properties"]["revenue"]["$ref"] == "#/properties/content/$defs/Amount"
    assert content["$defs"]["Amount"]["type"] == "object"
    assert bound["required"] == ["title", "content"]
    assert base["properties"]["content"] == {} and base["required"] == ["title"]
    bound["$defs"]["Amount"]["type"] = "number"
    assert base["$defs"]["Amount"]["type"] == "string"
    data = {"revenue": {"value": 120, "currency": "USD"}}
    validated = output().validate_content(data)
    data["revenue"]["value"] = 900
    assert validated["revenue"]["value"] == 120
    with pytest.raises(ValidationError):
        output().validate_content({"revenue": {"value": "120", "currency": "USD"}})


def test_output_aliases_round_trip_through_advertised_schema_and_storage():
    class Invoice(BaseModel):
        model_config = ConfigDict(extra="forbid")
        invoice_total: int = Field(alias="invoiceTotal")

    spec = OutputSpec("invoice", "1", Invoice)
    value = spec.build({"invoiceTotal": 120}, target=target(), context=context(), run_id="run-1")
    assert value.to_dict()["content"] == {"invoiceTotal": 120}
    assert spec.validate_content(value.content) == {"invoiceTotal": 120}


@pytest.mark.parametrize("options", [{"serialization_alias": "total"}, {"exclude": True}])
def test_output_rejects_models_whose_storage_shape_disagrees_with_input(options):
    class Invoice(BaseModel):
        model_config = ConfigDict(extra="forbid")
        invoice_total: int = Field(**options)

    with pytest.raises(ValueError, match="matching input and serialization"):
        OutputSpec("invoice", "1", Invoice)


# Generated by the ship coverage audit.
# Value: protects=registered output schema stays authoritative before persistence;
# fails_when=a mutated model silently replaces its registered contract;
# why_new=existing tests cover initial schemas and aliases, not later drift; seam=none.
def test_output_schema_drift_rejects_before_writer_invocation():
    class Content(BaseModel):
        model_config = ConfigDict(extra="forbid")
        value: int

    writes = []
    output_spec = OutputSpec("changing-content", "1", Content)
    registered_schema = output_spec.json_schema()
    writer = bind_output_capability(
        _spec(factory=lambda: writes.append),
        output_spec,
        target=target(),
        context=context(),
        run_id="run-1",
    ).materialize()
    Content.model_config["title"] = "Revised contract"
    Content.model_rebuild(force=True)
    assert Content.model_json_schema() != registered_schema
    assert output_spec.json_schema() == registered_schema
    with pytest.raises(HarnessError) as rejected:
        writer({"value": 120})
    assert rejected.value.code == "DOMAIN_SCHEMA_DRIFT"
    assert writes == []


# Generated by the ship coverage audit.
# Value: protects=bound outputs cannot be written under another active execution identity;
# fails_when=run or identity mismatch guards are removed;
# why_new=existing dispatch tests cover correct and absent active contexts; seam=none.
@pytest.mark.parametrize("changed", ["run_id", "tenant_id", "user_id", "session_id"])
def test_output_binding_rejects_conflicting_active_identity(monkeypatch, changed):
    writes = []
    writer = bind_output_capability(
        _spec(factory=lambda: writes.append),
        output(),
        target=target(),
        context=context(),
        run_id="run-1",
    ).materialize()
    active_context = context()
    active_run_id = "run-1"
    if changed == "run_id":
        active_run_id = "another-run"
    else:
        active_context = replace(active_context, **{changed: "another-identity"})
    monkeypatch.setattr(
        "agnoclaw.agent.get_current_tool_runtime",
        lambda: {"context": active_context, "parent_run_id": active_run_id},
    )
    with pytest.raises(HarnessError) as rejected:
        writer({"revenue": {"value": 120, "currency": "USD"}})
    assert rejected.value.code == "DOMAIN_RUN_CONTEXT_MISMATCH"
    assert writes == []


@pytest.mark.asyncio
async def test_cancelled_output_validation_closes_async_writer_without_starting_it(tmp_path):
    entered, release = threading.Event(), threading.Event()
    coroutines, writes, closed = [], [], []

    class SlowContent(BaseModel):
        model_config = ConfigDict(extra="forbid")
        value: int

        @model_validator(mode="after")
        def wait_for_release(self):
            entered.set()
            assert release.wait(timeout=5)
            return self

    class Writer:
        def __call__(self, product):
            async def persist():
                writes.append(product)

            pending = persist()
            coroutines.append(pending)
            return pending

        async def aclose(self):
            closed.append(True)

    spec = bind_output_capability(
        _spec(name="save.output", factory=Writer),
        OutputSpec("slow", "1", SlowContent),
        target=target(),
        context=context(),
        run_id="run-1",
    )
    executor, store, _ = _executor(tmp_path, spec)
    task = asyncio.create_task(
        executor.execute(
            "save.output",
            operation_id="save-1",
            run_id="run-1",
            attempt_id="attempt-1",
            arguments={"content": {"value": 120}},
            admission=_admission(),
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(coroutines) == 1 and not writes
        assert inspect.getcoroutinestate(coroutines[0]) == inspect.CORO_CLOSED
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await executor.aclose()
        store.close()
    assert closed == [True]


@pytest.mark.parametrize(
    "schema",
    [
        {},
        {"type": "object", "properties": {}},
        {"type": "object", "properties": {"content": {}}, "required": "content"},
    ],
)
def test_output_schema_binding_rejects_invalid_host_contract(schema):
    with pytest.raises(ValueError):
        bind_output_schema(schema, output())


def test_reports_require_exact_subject_and_version_and_never_treat_unknown_as_pass():
    digest = product().digest
    check = CheckResult("revenue.support", "1", digest, CheckStatus.PASS, "Supported.")
    report = VerificationReport(digest, (check,))
    report.require_pass(digest, ["revenue.support@1"])
    for required in ([], ["revenue.support@2"], ["missing@1"]):
        assert not report.passes(required)
    for status in (CheckStatus.FAIL, CheckStatus.UNKNOWN, CheckStatus.ERROR):
        assert not VerificationReport(digest, (replace(check, status=status),)).passes(
            ["revenue.support@1"]
        )
    with pytest.raises(HarnessError):
        report.require_pass(canonical_json_digest("another-output"), ["revenue.support@1"])
    with pytest.raises(ValueError):
        VerificationReport(digest, (check, check))


def test_checker_errors_are_bounded_and_do_not_leak_source_content():
    def broken():
        raise ValueError("private source text")

    result = run_check("support", "1", product().digest, broken)
    assert result.status is CheckStatus.ERROR
    assert "private" not in str(result.to_dict())


@pytest.mark.asyncio
async def test_nested_output_schema_and_writer_use_existing_governed_executor(tmp_path):
    products = []

    def persist(value):
        products.append(value)
        return {"product_digest": value.digest}

    spec = bind_output_capability(
        _spec(name="save.output", factory=lambda: persist),
        output(),
        target=target(),
        context=context(),
        run_id="run-1",
    )
    arguments = {"content": {"revenue": {"value": 120, "currency": "USD"}}}
    validate_capability_arguments(spec.input_schema, arguments, capability=spec.name)
    executor, store, _ = _executor(tmp_path, spec)
    try:
        execution = await executor.execute(
            "save.output@1.0.0",
            operation_id="save-1",
            run_id="run-1",
            attempt_id="attempt-1",
            arguments=arguments,
            admission=_admission(),
        )
        assert execution.value == {"product_digest": products[0].digest}
        assert products[0].metadata["artifact_type"] == "field_extraction"
        assert store.get_operation("save-1").state == "succeeded"
        with pytest.raises(HarnessError):
            await executor.execute(
                "save.output@1.0.0",
                operation_id="save-2",
                run_id="run-1",
                attempt_id="attempt-1",
                arguments={**arguments, "tenant_id": "tenant-2"},
                admission=_admission(),
            )
        assert len(products) == 1
    finally:
        await executor.aclose()
        store.close()


@pytest.mark.asyncio
async def test_output_binding_reads_actual_run_identity_through_harness_tool_dispatch(
    tmp_path,
    monkeypatch,
):
    from tests.test_agent_capabilities import ToolCallingAgent, _harness

    products, closed = [], []

    class Writer:
        async def __call__(self, value):
            products.append(value)
            return {"product_digest": value.digest}

        async def aclose(self):
            closed.append(True)

    spec = bind_output_capability(
        _spec(name="save.output", factory=Writer, required_scopes=("inventory:write",)),
        output(),
        target=target(),
    )
    monkeypatch.setattr(
        ToolCallingAgent, "arguments", {"content": {"revenue": {"value": 120, "currency": "USD"}}}
    )
    monkeypatch.setattr(ToolCallingAgent, "repeat", 1)
    harness, store = _harness(tmp_path, spec)
    try:
        run = await harness.start("Extract fields")
        await run.wait()
        assert products[0].run_id == run.run_id
        assert products[0].target.tenant_id == "tenant-1"
    finally:
        await harness.aclose()
        store.close()
    assert closed


@pytest.mark.asyncio
async def test_output_binding_cannot_guess_identity_outside_a_run(tmp_path):
    spec = bind_output_capability(_spec(factory=lambda: lambda _: None), output(), target=target())
    writer = spec.materialize()
    with pytest.raises(HarnessError, match="active governed"):
        writer({"revenue": {"value": 120, "currency": "USD"}})
