"""Exercise the short authoring journey, typing and scoped host integration."""

import asyncio
import inspect
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agnoclaw.domain import CheckOutcome, CheckStatus, Hook, Model, Pack
from agnoclaw.runtime import HarnessError
from tests.test_domain_core import context, target


class Metric(Model):
    amount: int = Field(ge=0)
    currency: str = "USD"


class Nested(Model):
    metric: Metric


class ExistingModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: int = Field(ge=0)


async def test_normal_typed_calls_infer_contracts_and_validate_before_handler():
    pack = Pack("metrics")
    called = []

    @pack.procedure
    async def extract(amount: int, /, *, currency: str = "USD") -> Metric:
        called.append(amount)
        return Metric(amount=amount, currency=currency)

    assert inspect.signature(extract) == inspect.signature(extract.function)
    assert inspect.isfunction(extract) and inspect.iscoroutinefunction(extract)
    assert await extract(3) == Metric(amount=3)
    with pytest.raises(ValidationError, match="amount"):
        await extract("3")
    with pytest.raises(TypeError, match="unexpected keyword"):
        await extract(3, unknown=True)
    assert called == [3]
    compiled = pack.compile()
    assert compiled.procedure("metrics.extract") is extract.definition
    assert compiled.schema("metrics.Metric") is extract.output
    assert (
        "schema/metrics.extract.input@1"
        in compiled.definition("operation", extract.definition.ref).dependencies
    )


async def test_return_values_are_revalidated_including_constructed_or_mutated_models():
    pack = Pack("invalid")

    @pack.procedure
    def invalid() -> Metric:
        return Metric.model_construct(amount=-1)

    with pytest.raises(ValidationError, match="greater than or equal"):
        await invalid()


async def test_typed_checks_fail_closed_without_fabricating_object_identity():
    pack = Pack("checked")

    @pack.procedure
    def extract(amount: int) -> Metric:
        return Metric(amount=amount)

    @extract.check
    def reasonable(metric: Metric):
        return CheckOutcome(
            CheckStatus.PASS if metric.amount < 100 else CheckStatus.UNKNOWN, "Check the source"
        )

    assert reasonable(Metric(amount=1)).status is CheckStatus.PASS
    assert (await extract(2)).amount == 2
    with pytest.raises(HarnessError, match="reasonable.*unknown"):
        await extract(100)
    async with pack.run(target(), context=context(), run_id="run-1") as run:
        with pytest.raises(HarnessError, match="reasonable.*unknown"):
            await extract(100)
        assert not run.last.ready
        assert run.last.report.results[-1].status is CheckStatus.UNKNOWN


async def test_examples_execute_the_same_function_and_pin_the_full_contract():
    pack = Pack("examples")

    @pack.procedure
    def extract(amount: int, /, *, currency: str = "USD") -> Metric:
        return Metric(amount=amount, currency=currency)

    extract.example(5, expected=Metric(amount=5))
    extract.example(6, currency="EUR", expected={"amount": 6, "currency": "EUR"})
    assert (await pack.test()).passed
    with pytest.raises(ValueError, match="before its first call"):
        extract.example(7, expected=Metric(amount=7))
    assert pack.compile().get("evaluation", "examples.extract.examples").subjects == (
        pack.compile().get("operation", extract.definition.ref),
    )


async def test_failed_example_names_the_procedure_and_case():
    pack = Pack("fixtures")

    @pack.procedure
    def extract(amount: int) -> Metric:
        return Metric(amount=amount)

    extract.example(1, expected=Metric(amount=2))
    with pytest.raises(AssertionError, match="fixtures.extract:example-1: fail"):
        await pack.test()


async def test_request_context_is_bound_once_and_isolated_across_tenants():
    pack = Pack("requests")

    @pack.procedure
    async def extract(amount: int) -> Metric:
        await asyncio.sleep(0)
        return Metric(amount=amount)

    async def request(tenant, amount):
        ref = replace(target(), tenant_id=tenant)
        async with pack.run(ref, context=context(tenant), run_id=tenant) as run:
            value = await extract(amount)
            receipt = run.last
            value.amount = 999
            assert receipt.product.content["amount"] == amount
        with pytest.raises(RuntimeError, match="cannot be reused"):
            async with run:
                pass
        return receipt.product.target.tenant_id, receipt.product.run_id

    assert await asyncio.gather(request("a", 1), request("b", 2)) == [("a", "a"), ("b", "b")]
    assert (await extract(3)).amount == 3


async def test_hooks_cannot_be_bypassed_by_a_direct_call_and_validate_nested_output():
    pack = Pack("hooks")
    called = []

    @pack.procedure
    def extract(amount: int) -> Nested:
        called.append(amount)
        return Nested(metric=Metric(amount=amount))

    pack.add(
        Hook(
            "adjust",
            extract.output,
            lambda values, ctx: {"metric": {"amount": 10}},
            mode="transform",
            implementation_id="1",
        )
    )
    with pytest.raises(ValueError, match="context hooks require"):
        await extract(1)
    assert called == []
    async with pack.run(target(), context=context(), run_id="run") as run:
        result = await extract(1)
        assert result.metric.amount == 10
        assert run.last.product.content["metric"]["amount"] == 10


async def test_nested_scopes_restore_the_previous_run_and_cancelled_calls_leave_no_receipt():
    pack = Pack("scopes")

    @pack.procedure(timeout_seconds=0.01)
    async def extract(amount: int, wait: bool = False) -> Metric:
        if wait:
            await asyncio.sleep(10)
        return Metric(amount=amount)

    async with pack.run(target(), context=context(), run_id="outer") as outer:
        async with pack.run(target(), context=context(), run_id="inner") as inner:
            await extract(1)
            with pytest.raises(TimeoutError):
                await extract(2, wait=True)
            assert len(inner.results) == 1
        await extract(3)
        assert outer.last.product.run_id == "outer"
    assert (await extract(4)).amount == 4


async def test_included_packs_keep_their_definitions_and_freeze_before_activation():
    common = Pack("common")

    @common.procedure
    def extract(amount: int) -> Metric:
        return Metric(amount=amount)

    application = Pack("app", includes=(common,))
    application.alias("builtin:metric", Metric)
    async with application.run(target(), context=context(), run_id="run") as run:
        assert (await extract(1)).amount == 1
        assert run.last.procedure_reference == "common.extract@1"
    with pytest.raises(ValueError, match="before its first call"):
        extract.check(lambda value: True)


def test_registration_errors_identify_the_missing_annotation_and_duplicate_name():
    pack = Pack("bad")

    def missing(amount) -> Metric:
        return Metric(amount=amount)

    with pytest.raises(TypeError, match="annotate parameter 'amount'"):
        pack.procedure(missing)

    @pack.procedure
    def extract(amount: int) -> Metric:
        return Metric(amount=amount)

    with pytest.raises(ValueError, match="already registered"):
        pack.procedure(extract.function)


def test_active_tool_identity_is_reused_and_explicit_conflicts_are_rejected(monkeypatch):
    pack = Pack("tool")

    @pack.procedure
    def extract(amount: int) -> Metric:
        return Metric(amount=amount)

    with pytest.raises(ValueError, match="needs context="):
        pack.run(target())
    monkeypatch.setattr(
        "agnoclaw.agent.get_current_tool_runtime",
        lambda: {"context": context(), "parent_run_id": "tool-run"},
    )
    run = pack.run(target())
    assert run.context == context() and run.run_id == "tool-run"
    with pytest.raises(HarnessError, match="Explicit identity"):
        pack.run(target(), context=context("other"), run_id="other")


def test_type_checker_preserves_async_decorator_parameters_and_model_result(tmp_path):
    source = tmp_path / "typed_pack.py"
    source.write_text("""from typing import assert_type
from agnoclaw.domain import Model, Pack
pack = Pack("typed")
class Metric(Model):
    amount: int
@pack.procedure
async def extract(text: str) -> Metric:
    return Metric(amount=len(text))
@pack.procedure(timeout_seconds=10)
def calculate(amount: int) -> Metric:
    return Metric(amount=amount)
@pack.procedure(timeout_seconds=10)
async def configured(text: str) -> Metric:
    return Metric(amount=len(text))
@pack.procedure
def plain(amount: int) -> Metric:
    return Metric(amount=amount)
@extract.check
def nonnegative(value: Metric) -> bool:
    return value.amount >= 0
async def check() -> None:
    assert_type(await extract("text"), Metric)
    assert_type(await calculate(2), Metric)
    assert_type(await configured("text"), Metric)
    assert_type(await plain(2), Metric)
    assert_type(nonnegative(Metric(amount=2)), bool)
    await extract(2)  # type: ignore[arg-type]
    await calculate("2")  # type: ignore[arg-type]
    await configured(2)  # type: ignore[arg-type]
    await plain("2")  # type: ignore[arg-type]
""")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--follow-imports=silent",
            "--warn-unused-ignores",
            str(source),
        ],
        cwd=Path(__file__).parents[1],
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def test_diamond_includes_deduplicate_examples_and_inherit_pack_version():
    common = Pack("common", version="2")
    called = []

    @common.procedure
    def extract(amount: int) -> Metric:
        called.append(amount)
        return Metric(amount=amount)

    extract.example(5, expected=Metric(amount=5))
    app = Pack("app", includes=(Pack("left", includes=(common,)), common))
    compiled = app.compile()
    assert called == []
    assert compiled.procedure("common.extract").version == "2"
    assert compiled.schema("common.Metric").version == "2"
    assert (await app.test()).passed
    assert called == [5]


async def test_inherited_scope_cannot_outlive_its_host_request():
    pack = Pack("lifetime")
    started, finish = asyncio.Event(), asyncio.Event()

    @pack.procedure
    async def extract() -> Metric:
        started.set()
        await finish.wait()
        return Metric(amount=1)

    async with pack.run(target(), context=context(), run_id="run") as run:
        pending = asyncio.create_task(extract())
        await started.wait()
    finish.set()
    with pytest.raises(RuntimeError, match="not active"):
        await pending
    assert run.results == ()


async def test_cancellation_is_propagated_and_does_not_create_a_success_receipt():
    pack = Pack("cancel")
    started = asyncio.Event()

    @pack.procedure
    async def extract() -> Metric:
        started.set()
        await asyncio.sleep(10)
        return Metric(amount=1)

    async with pack.run(target(), context=context(), run_id="run") as run:
        pending = asyncio.create_task(extract())
        await started.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert run.results == ()


def test_scope_rejects_another_tenant_before_dispatch():
    pack = Pack("scope")

    @pack.procedure
    def extract() -> Metric:
        pytest.fail("scope validation must happen before dispatch")

    with pytest.raises(HarnessError):
        pack.run(target(), context=context("other"), run_id="run")


def test_trusted_directory_loader_accepts_function_packs(tmp_path):
    from agnoclaw.packs import PackTrustError, load_pack

    (tmp_path / "agnoclaw-pack.toml").write_text("""name = "typed-pack"
[provides]
domains = ["typed_pack_fixture:register"]
""")
    (tmp_path / "typed_pack_fixture.py").write_text("""from agnoclaw.domain import Pack, Model
pack = Pack("loaded")
class Value(Model):
    amount: int
@pack.procedure
def extract(amount: int) -> Value:
    raise AssertionError("loading must not run the handler")
def register():
    return pack
""")
    with pytest.raises(PackTrustError):
        load_pack(tmp_path)
    loaded = load_pack(tmp_path, trusted=True)
    assert loaded.domains[0].procedure("loaded.extract").output.name == "loaded.Value"


async def test_nested_input_instances_are_checked_before_the_handler_runs():
    pack = Pack("inputs")
    called = []

    @pack.procedure
    def extract(value: ExistingModel) -> Metric:
        called.append(value)
        return Metric(amount=value.amount)

    with pytest.raises(ValidationError, match="greater than or equal"):
        await extract(ExistingModel.model_construct(amount=-1))
    assert called == []


def test_short_example_runs_as_an_ordinary_script():
    result = subprocess.run(
        [
            sys.executable,
            "examples/clarion_domain_pack/clarion_domain/simple.py",
        ],
        cwd=Path(__file__).parents[1],
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"amount_minor":12000' in result.stdout


async def test_decorated_function_registers_with_agno_without_an_adapter():
    from agno.tools.function import Function

    pack = Pack("agno")

    def extract(amount: int) -> Metric:
        return Metric(amount=amount)

    # Resolve postponed annotations in the author's module, not pack.py's globals.
    extract.__annotations__ = {"amount": "int", "return": "Metric"}
    procedure = pack.procedure(extract, name="agno.extract")

    @procedure.check
    def reasonable(value: Metric) -> bool:
        return value.amount < 100

    tool = Function.from_callable(procedure)
    assert tool.parameters["properties"]["amount"]["type"] == "integer"
    assert inspect.iscoroutinefunction(tool.entrypoint)
    assert (await tool.entrypoint(amount=3)).amount == 3
    with pytest.raises(HarnessError, match="reasonable.*fail"):
        await tool.entrypoint(amount=100)
