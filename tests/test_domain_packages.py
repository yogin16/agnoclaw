"""Developer journeys and cross-definition guarantees for composed domain packages."""

import json
from dataclasses import replace

import pytest
from pydantic import BaseModel, ConfigDict

from agnoclaw.capabilities import CapabilityRecovery, CapabilityRegistry
from agnoclaw.capability_execution import CapabilityExecutor
from agnoclaw.domain import (
    Check,
    Domain,
    DomainValidationError,
    Entity,
    EvaluationCase,
    EvaluationSuite,
    FeedbackKind,
    Interface,
    ObjectId,
    ObjectSnapshot,
    Override,
    Procedure,
    Relation,
    Resources,
    Schema,
    SkillBinding,
    Workflow,
    WorkflowPlan,
    WorkflowStep,
    compare_schemas,
    inspect_domain_manifest,
)
from agnoclaw.runtime import HarnessError, OperationGateway, OperationState, OperationTerminalError
from agnoclaw.runtime.operations import EffectClass
from agnoclaw.runtime.store import OperationIdempotencyConflictError, SQLiteRuntimeStore
from tests.test_capability_execution import _admission, _executor, _spec
from tests.test_domain_core import context, target


class Value(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: int


def positive(work):
    return work.content["value"] > 0


def definitions():
    schema = Schema("metric", Value)
    skill = SkillBinding("extract", schema, resource="skills/extract/SKILL.md")
    procedure = Procedure(
        "extract-metric", schema, skill=skill, checks=(Check("positive", positive),)
    )
    return schema, skill, procedure


def test_compile_discovers_dependencies_and_preserves_host_schema_names():
    schema, skill, procedure = definitions()
    package = Domain(
        "clarion",
        (procedure,),
        components=(Resources("skills", (skill.resource,)),),
        aliases={"builtin:company_metric": schema},
    )
    compiled = package.compile()
    assert compiled.procedure("extract-metric") is procedure
    assert compiled.schema("builtin:company_metric") is schema
    assert compiled.use(skill) is skill
    assert len(compiled.definitions) == 5
    assert package.validate().valid
    manifest = compiled.manifest()
    assert inspect_domain_manifest(json.loads(json.dumps(manifest))) == manifest
    manifest["definitions"][0]["details"]["arbitrary"] = "detached"
    assert manifest != compiled.manifest()
    with pytest.raises(TypeError):
        compiled.definitions["new"] = None
    assert procedure.spec.skill == "extract"


def test_compile_aggregates_missing_skills_resources_and_conflicting_schemas():
    schema, skill, procedure = definitions()
    conflict = Schema("metric", Value, metadata={"different": True})
    unbound = replace(procedure, name="unbound", skill="missing")
    package = Domain("bad", components=(procedure, conflict, unbound, object()))
    report = package.validate()
    codes = {item.code for item in report.diagnostics}
    assert {
        "DEFINITION_CONFLICT",
        "RESOURCE_UNDECLARED",
        "SKILL_UNRESOLVED",
        "INVALID_DEFINITION",
    } <= codes
    with pytest.raises(DomainValidationError) as error:
        package.compile()
    assert error.value.diagnostics == report.diagnostics
    assert "bad.definitions" in str(error.value)
    assert "first defined at" in str(error.value)


def test_catalog_does_not_execute_callbacks_and_rejects_ambiguous_versions():
    def forbidden(_):
        raise AssertionError("compilation must not run a checker")

    schema = Schema("metric", Value)
    procedure = Procedure("extract", schema, checks=(Check("no-call", forbidden),))
    package = Domain("pack", components=(procedure, replace(schema, version="2")))
    compiled = package.compile()
    with pytest.raises(DomainValidationError, match="AMBIGUOUS_REFERENCE"):
        compiled.schema("metric")
    assert compiled.schema(schema.ref) is schema


def test_direct_skill_reference_selects_one_of_multiple_installed_versions():
    schema = Schema("metric", Value)
    skill = SkillBinding("extract", schema)
    procedure = Procedure(
        "extract-metric", schema, skill=skill, checks=(Check("positive", positive),)
    )
    compiled = Domain("pack", (procedure,), components=(replace(skill, version="2"),)).compile()
    assert compiled.procedure("extract-metric").skill is skill
    assert "skill/extract@1" in compiled.definition("procedure", procedure.ref).dependencies


def test_host_binding_diagnostics_never_materialize_factories():
    spec = _spec(factory=lambda: (_ for _ in ()).throw(AssertionError("not during inspection")))
    compiled = Domain("host", components=(spec,)).compile()
    registry = CapabilityRegistry()
    assert compiled.validate_host(registry).diagnostics[0].code == "HOST_BINDING_MISSING"
    registry.register(spec)
    compiled.validate_host(registry).require_valid()
    missing_factory = CapabilityRegistry()
    missing_factory.register(replace(spec, factory=None))
    assert compiled.validate_host(missing_factory).diagnostics[0].code == "HOST_FACTORY_MISSING"
    drifted = CapabilityRegistry()
    drifted.register(replace(spec, implementation_digest="sha256:" + "0" * 64))
    assert compiled.validate_host(drifted).diagnostics[0].code == "HOST_BINDING_CHANGED"


async def test_prepared_feedback_automatically_binds_procedure_and_subject():
    schema = Schema("value", Value)
    procedure = Procedure("extract", schema, checks=(Check("positive", positive),))
    prepared = await procedure.prepare(
        {"value": 1}, target=target(), run_id="run", context=context()
    )
    feedback = prepared.feedback(
        context=context(), kind=FeedbackKind.CORRECTION, payload={"value": 2}
    )
    assert feedback.procedure_digest == procedure.digest
    assert feedback.product_digest == prepared.product.digest
    assert feedback.regression_case({"value": 1}).expected == {"value": 2}
    with pytest.raises(HarnessError, match="tenant"):
        prepared.feedback(
            context=context("other"), kind=FeedbackKind.CORRECTION, payload={"value": 2}
        )


def test_explicit_override_checks_prior_digest_and_dependent_bindings():
    schema = Schema("metric", Value)
    base = Domain("base", components=(schema,))
    replacement = replace(schema, metadata={"label": "new"})
    updated = Domain(
        "overlay", includes=(base,), overrides=(Override(replacement, schema.digest, "label"),)
    )
    assert updated.compile().schema("metric") is replacement
    with pytest.raises(DomainValidationError, match="OVERRIDE_STALE"):
        replace(updated, overrides=(Override(replacement, replacement.digest, "stale"),)).compile()
    procedure = Procedure("extract", schema, checks=(Check("positive", positive),))
    with pytest.raises(DomainValidationError, match="DEPENDENCY_CHANGED"):
        replace(updated, includes=(Domain("base", (procedure,)),)).compile()


def test_transitive_diff_and_pinned_evaluation_evidence():
    schema = Schema("metric", Value)
    entity = Entity("company", schema)
    before = Domain("pack", components=(entity,)).compile()
    after = Domain(
        "pack", components=(replace(entity, schema=replace(schema, version="2")),)
    ).compile()
    diff = after.diff(before)
    assert "schema/metric@1" in diff.removed
    assert "schema/metric@2" in diff.added
    assert "entity/company@1" in diff.changed
    assert "entity/company@1" in diff.affected


def test_included_package_versions_are_pinned_and_conflicting_package_ids_fail():
    schema = Schema("metric", Value)
    base = Domain("base", components=(schema,))
    before = Domain("application", includes=(base,)).compile()
    after = Domain("application", includes=(replace(base, version="2"),)).compile()
    assert before.digest != after.digest
    assert after.diff(before).pack_changed
    assert "base@2" in after.manifest()["packages"]
    conflict = Domain("base", components=(replace(schema, name="different"),))
    with pytest.raises(DomainValidationError, match="PACK_CONFLICT"):
        Domain("application", includes=(base, conflict)).compile()


async def test_pack_evaluation_uses_existing_cases_and_binds_manifest():
    schema = Schema("metric", Value)

    async def run(inputs):
        return schema.validate_content(inputs)

    suite = EvaluationSuite(
        "metric-fixtures",
        (EvaluationCase("valid", {"value": 1}, {"value": 1}),),
        run,
        implementation_id="fixture-v1",
        subjects=(schema,),
    )
    compiled = Domain("pack", components=(suite,)).compile()
    report = await compiled.evaluate()
    assert report.passed
    report.require_current(compiled)
    changed = Domain("pack", version="2", components=(suite,)).compile()
    with pytest.raises(HarnessError, match="different pack"):
        report.require_current(changed)


def test_manifest_inspection_rejects_unresolved_dependencies_and_cycles():
    schema = Schema("metric", Value)
    compiled = Domain("pack", components=(Entity("company", schema),)).compile()
    for field, value in (("format", True), ("definitions", {}), ("aliases", []), ("packages", [])):
        with pytest.raises(ValueError, match="manifest"):
            inspect_domain_manifest({**compiled.manifest(), field: value})
    manifest = compiled.manifest()
    manifest["definitions"] = [d for d in manifest["definitions"] if d["kind"] != "schema"]
    with pytest.raises(ValueError, match="unresolved"):
        inspect_domain_manifest(manifest)
    manifest = compiled.manifest()
    item = manifest["definitions"][0]
    item["dependencies"][f"{item['kind']}/{item['reference']}"] = item["fingerprint"]
    with pytest.raises(ValueError, match="cycle"):
        inspect_domain_manifest(manifest)


def test_relation_validation_distinguishes_partial_view_and_known_violation():
    schema = Schema("metric", Value)
    company, deal = Entity("company", schema), Entity("deal", schema)
    relation = Relation("belongs-to", deal, company, max_targets=1)
    source = ObjectSnapshot(ObjectId("tenant", "deal", "d1").at("1"), {"value": 1})
    a = ObjectSnapshot(ObjectId("tenant", "company", "c1").at("1"), {"value": 2})
    b = ObjectSnapshot(ObjectId("tenant", "company", "c2").at("1"), {"value": 3})
    first, second = relation.link(source, a), relation.link(source, b)
    assert relation.verify(first).results[-1].status == "unknown"
    assert relation.verify(first, complete=True).results[-1].status == "pass"
    assert relation.verify(first, neighbors=(second,)).results[-1].status == "fail"
    with pytest.raises(HarnessError, match="tenant"):
        relation.link(source, replace(a, reference=replace(a.reference, tenant_id="other")))


def test_schema_compatibility_reports_direction_and_custom_validator_uncertainty():
    class Expanded(Value):
        label: str

    old, new = Schema("value", Value), Schema("value", Expanded, version="2")
    assert compare_schemas(old, new).status == "incompatible"
    assert compare_schemas(old, new, direction="write_old").status == "incompatible"
    assert compare_schemas(old, Schema("value", Value)).status == "unknown"


def test_schema_overlay_requires_explicit_version_and_resolved_field_types():
    base = Schema("value", Value)
    overlay = base.extend({"label": (str, "pending")}, version="2")
    assert overlay.validate_content({"value": 1}) == {"value": 1, "label": "pending"}
    assert base.validate_content({"value": 1}) == {"value": 1}
    with pytest.raises(ValueError, match="new version"):
        base.extend({"label": (str, "")}, version="1")
    with pytest.raises(ValueError, match="already exist"):
        base.extend({"value": (str, "")}, version="2")
    with pytest.raises(ValueError, match="resolved Python types"):
        base.extend({"label": ("str", "")}, version="2")
    before = Domain("pack", components=(base, overlay), aliases={"builtin:value": base}).compile()
    after = Domain("pack", components=(base, overlay), aliases={"builtin:value": overlay}).compile()
    assert after.diff(before).aliases_changed == ("builtin:value",)


async def test_workflow_runs_through_real_executor_replays_and_rejects_definition_drift(tmp_path):
    calls = []
    schema = Schema("value", Value)
    interface = Interface("increment", schema, schema)

    def increment(value):
        calls.append(value)
        return {"value": value + 1}

    spec = _spec(input_schema=schema.json_schema(), factory=lambda: increment)
    binding = interface.bind(spec)
    workflow = Workflow(
        "calculate", (WorkflowStep("first", binding), WorkflowStep("second", binding))
    )
    plan = workflow.plan({"value": 1}, tenant_id="tenant-1", request_id="request-1")
    executor, store, _ = _executor(tmp_path, spec)
    kwargs = dict(run_id="run-1", attempt_id="attempt-1", admission=_admission())
    try:
        assert await workflow.execute(executor, plan, **kwargs) == {"value": 3}
        assert await workflow.execute(executor, plan, **kwargs) == {"value": 3}
        assert calls == [1, 2]
        # Rebuilding a plan under the same request ID cannot adopt new semantics,
        # even when the capability and its arguments did not change.
        upgraded = replace(workflow, version="2")
        replacement_plan = upgraded.plan({"value": 1}, tenant_id="tenant-1", request_id="request-1")
        with pytest.raises(OperationIdempotencyConflictError):
            await upgraded.execute(executor, replacement_plan, **kwargs)
        assert calls == [1, 2]
        changed = replace(
            workflow,
            steps=(
                WorkflowStep(
                    "first",
                    replace(
                        binding,
                        capability=replace(spec, implementation_digest="sha256:" + "a" * 64),
                    ),
                ),
            ),
        )
        with pytest.raises(HarnessError, match="pinned workflow"):
            await changed.execute(executor, plan, **kwargs)
        with pytest.raises(HarnessError, match="tenant"):
            await workflow.execute(executor, replace(plan, tenant_id="other"), **kwargs)
    finally:
        await executor.aclose()
        store.close()


async def test_workflow_reopens_persisted_store_without_redispatch(tmp_path):
    schema = Schema("value", Value)
    calls = []
    spec = _spec(
        input_schema=schema.json_schema(),
        factory=lambda: lambda value: calls.append(value) or {"value": value},
    )
    binding = Interface("echo", schema, schema).bind(spec)
    workflow = Workflow("resume", (WorkflowStep("echo", binding),))
    plan = workflow.plan({"value": 4}, tenant_id="tenant-1", request_id="same-request")
    executor, store, artifacts = _executor(tmp_path, spec, artifact_results=True)
    kwargs = dict(run_id="run-1", attempt_id="attempt-1", admission=_admission())
    await workflow.execute(executor, plan, **kwargs)
    await executor.aclose()
    store.close()
    plan = WorkflowPlan(**json.loads(json.dumps(plan.to_dict())))
    reopened = SQLiteRuntimeStore(tmp_path / "runtime.db")
    registry = CapabilityRegistry()
    registry.register(spec)
    second = CapabilityExecutor(
        registry,
        OperationGateway(
            reopened, worker_id="restarted", artifact_store=artifacts, result_cache_size=0
        ),
    )
    try:
        assert await workflow.execute(second, plan, **kwargs) == {"value": 4}
        assert calls == [4]
    finally:
        await second.aclose()
        reopened.close()


async def test_workflow_and_step_names_cannot_alias_another_operation(tmp_path):
    schema = Schema("value", Value)
    calls = []
    spec = _spec(
        input_schema=schema.json_schema(),
        factory=lambda: lambda value: calls.append(value) or {"value": value},
    )
    binding = Interface("echo", schema, schema).bind(spec)
    executor, store, _ = _executor(tmp_path, spec)
    try:
        for name, step in (("a:b", "c"), ("a", "b:c")):
            flow = Workflow(name, (WorkflowStep(step, binding),))
            plan = flow.plan({"value": 4}, tenant_id="tenant-1", request_id="same-request")
            await flow.execute(
                executor, plan, run_id="run-1", attempt_id="attempt-1", admission=_admission()
            )
        assert calls == [4, 4]
    finally:
        await executor.aclose()
        store.close()


async def test_effectful_binding_requires_host_guard_before_materializing(tmp_path):
    schema = Schema("value", Value)
    calls = []
    spec = _spec(
        input_schema=schema.json_schema(),
        effect=EffectClass.IDEMPOTENT,
        factory=lambda: calls.append("factory") or (lambda value: {"value": value}),
    )
    binding = Interface("write", schema, schema).bind(spec)
    executor, store, _ = _executor(tmp_path, spec)
    try:
        with pytest.raises(HarnessError, match="pre-dispatch guard"):
            await binding.execute(
                executor,
                {"value": 1},
                operation_id="op",
                run_id="run-1",
                attempt_id="attempt-1",
                admission=_admission(),
            )
        assert calls == []
    finally:
        await executor.aclose()
        store.close()


async def test_workflow_does_not_repeat_uncertain_effect_after_reopening_store(tmp_path):
    schema = Schema("value", Value)
    calls = []

    def write(value):
        calls.append("write")
        raise TimeoutError("provider accepted the write, response was lost")

    read = _spec(
        input_schema=schema.json_schema(),
        factory=lambda: lambda value: calls.append("read") or {"value": value},
    )
    effect = _spec(
        name="metric.publish",
        input_schema=schema.json_schema(),
        effect=EffectClass.NON_REPEATABLE,
        recovery=CapabilityRecovery.RECONCILABLE,
        factory=lambda: write,
    )
    workflow = Workflow(
        "publish",
        (
            WorkflowStep("read", Interface("read", schema, schema).bind(read)),
            WorkflowStep("write", Interface("write", schema, schema).bind(effect)),
        ),
    )
    plan = workflow.plan({"value": 1}, tenant_id="tenant-1", request_id="request")
    executor, store, artifacts = _executor(tmp_path, read, effect, artifact_results=True)
    kwargs = dict(
        run_id="run-1",
        attempt_id="attempt-1",
        admission=_admission(),
        pre_dispatch={"write": lambda: None},
    )
    try:
        with pytest.raises(OperationTerminalError) as error:
            await workflow.execute(executor, plan, **kwargs)
        assert error.value.record.state is OperationState.UNKNOWN
    finally:
        await executor.aclose()
        store.close()

    reopened = SQLiteRuntimeStore(tmp_path / "runtime.db")
    registry = CapabilityRegistry()
    registry.register(read)
    registry.register(effect)
    second = CapabilityExecutor(
        registry,
        OperationGateway(reopened, worker_id="restarted", artifact_store=artifacts),
    )
    try:
        with pytest.raises(OperationTerminalError) as error:
            await workflow.execute(second, plan, **kwargs)
        assert error.value.record.state is OperationState.UNKNOWN
        assert calls == ["read", "write"]
    finally:
        await second.aclose()
        reopened.close()
