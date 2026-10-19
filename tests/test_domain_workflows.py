"""Adversarial domain review, routing, context, feedback and pack composition cases."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from agnoclaw.domain import (
    ActionSpec,
    CheckResult,
    CheckStatus,
    ContextContribution,
    ContextContributor,
    DecisionRule,
    DecisionStatus,
    DecisionTable,
    DomainPack,
    EvaluationCase,
    FeedbackEvent,
    FeedbackKind,
    InferencePolicy,
    InferenceRoute,
    ProcedureSpec,
    ReviewRequest,
    ReviewVerdict,
    VerificationReport,
    assemble_context,
    evaluate_cases,
)
from agnoclaw.packs import PackError, PackTrustError, inspect_pack, load_pack
from agnoclaw.runtime import EffectClass, HarnessError
from agnoclaw.runtime.security import canonical_json_digest, thaw_data
from tests.test_capability_execution import _admission, _executor, _spec
from tests.test_domain_core import context, output, product, target

NOW = datetime(2026, 10, 18, tzinfo=UTC)


def report(subject, status=CheckStatus.PASS):
    return VerificationReport(
        subject, (CheckResult("supported", "1", subject, status, "Checked."),)
    )


def action(factory=lambda: lambda **kwargs: kwargs):
    return ActionSpec(_spec(factory=factory), ("supported@1",))


def proposal():
    return action().propose(product(), {"value": 120}, context=context())


def review(value, checks):
    request = ReviewRequest(
        value.target,
        value.digest,
        checks.digest,
        NOW + timedelta(hours=1),
        "deals:review",
        "Update revenue",
        {"value": {"before": None, "after": 120}},
    )
    return request.decide(
        ReviewVerdict.APPROVE, context=replace(context(), scopes=("deals:review",)), now=NOW
    )


def register_domain():
    return DomainPack(
        "diligence",
        "1",
        (
            ProcedureSpec(
                "extract", "1", "deal-field-extraction", output(), ("supported@1",), (action(),)
            ),
        ),
    )


def test_review_requires_authenticated_scope_and_cannot_be_reused_for_changed_evidence():
    value = proposal()
    checks = report(value.digest)
    approved = review(value, checks)
    with pytest.raises(HarnessError, match="scope"):
        approved.request.decide(ReviewVerdict.APPROVE, context=context(), now=NOW)
    action().preflight(
        value, context=context(), current_target=target(), report=checks, review=approved, now=NOW
    )
    changed = VerificationReport(value.digest, (replace(checks.results[0], reason="Recomputed."),))
    with pytest.raises(HarnessError, match="exact"):
        action().preflight(
            value,
            context=context(),
            current_target=target(),
            report=changed,
            review=approved,
            now=NOW,
        )


@pytest.mark.parametrize(
    "failure",
    [
        "stale",
        "foreign",
        "missing",
        "expired",
        "rejected",
        "arguments",
        "capability",
        "unknown",
        "wrong_subject",
    ],
)
def test_action_preflight_rejects_unsafe_or_unverified_proposals(failure):
    spec, value = action(), proposal()
    checks = report(value.digest)
    approved = review(value, checks)
    kwargs = dict(
        context=context(), current_target=target(), report=checks, review=approved, now=NOW
    )
    if failure == "stale":
        kwargs["current_target"] = target("2")
    elif failure == "foreign":
        kwargs["context"] = context("other-tenant")
    elif failure == "missing":
        kwargs["review"] = None
    elif failure == "expired":
        kwargs["now"] = NOW + timedelta(hours=2)
    elif failure == "rejected":
        kwargs["review"] = replace(approved, verdict=ReviewVerdict.REJECT)
    elif failure == "arguments":
        value = replace(value, arguments={"value": 999})
    elif failure == "capability":
        spec = replace(
            spec,
            capability=replace(
                spec.capability,
                implementation_digest="sha256:new",
                input_schema=thaw_data(spec.capability.input_schema),
            ),
        )
    elif failure == "unknown":
        kwargs["report"] = report(value.digest, CheckStatus.UNKNOWN)
    elif failure == "wrong_subject":
        kwargs["report"] = report(product().digest)
    with pytest.raises(HarnessError):
        spec.preflight(value, **kwargs)


@pytest.mark.asyncio
async def test_existing_executor_pre_dispatch_prevents_effects_and_replays_idempotent_result(
    tmp_path,
):
    calls = []

    def mutate(**arguments):
        calls.append(arguments)
        return {"committed": True}

    capability = _spec(factory=lambda: mutate, effect=EffectClass.IDEMPOTENT)
    spec = ActionSpec(capability, ("supported@1",))
    value = spec.propose(product(), {"value": 120}, context=context())
    checks = report(value.digest)
    approved = review(value, checks)
    current = target("2")

    def preflight():
        spec.preflight(
            value,
            context=context(),
            current_target=current,
            report=checks,
            review=approved,
            now=NOW,
        )

    executor, store, _ = _executor(tmp_path, capability)
    try:
        with pytest.raises(HarnessError):
            await executor.execute(
                spec.reference,
                operation_id="blocked",
                run_id="run-1",
                attempt_id="attempt-1",
                arguments=thaw_data(value.arguments),
                admission=_admission(),
                pre_dispatch=preflight,
                idempotency_key="blocked-key",
            )
        assert not calls
        current = target()
        for _ in range(2):
            await executor.execute(
                spec.reference,
                operation_id="approved",
                run_id="run-1",
                attempt_id="attempt-1",
                arguments=thaw_data(value.arguments),
                admission=_admission(),
                pre_dispatch=preflight,
                idempotency_key=value.digest,
            )
        assert len(calls) == 1
    finally:
        await executor.aclose()
        store.close()


def test_decision_table_abstains_on_no_match_conflict_and_non_boolean_or_failed_rules():
    rules = (
        DecisionRule("small", "routine", lambda facts: facts["amount"] < 100),
        DecisionRule("large", "review", lambda facts: facts["amount"] >= 100),
    )
    table = DecisionTable("amount-band", "1", canonical_json_digest("rules-v1"), rules)
    assert table.evaluate({"amount": 50}).outcome == "routine"
    assert table.evaluate({"amount": 100}).outcome == "review"
    assert table.evaluate({}).status is DecisionStatus.ERROR
    assert (
        replace(table, rules=rules[:1]).evaluate({"amount": 100}).status is DecisionStatus.NO_MATCH
    )
    conflict = replace(table, rules=(*rules, DecisionRule("always", "routine", lambda _: True)))
    assert conflict.evaluate({"amount": 50}).status is DecisionStatus.CONFLICT
    assert conflict.evaluate({"amount": 50}).outcome is None
    wrong = replace(table, rules=(DecisionRule("bad", "routine", lambda _: 1),))
    assert wrong.evaluate({}).status is DecisionStatus.ERROR


def test_fast_routing_is_opt_in_and_checks_escalate_once_then_request_review():
    kwargs = {"required_checks": ("supported@1",)}
    assert InferencePolicy().choose(**kwargs).route is InferenceRoute.DELIBERATE
    policy = InferencePolicy(fast_path_validated=True)
    assert policy.choose(**kwargs).route is InferenceRoute.FAST
    assert policy.choose(**kwargs, ambiguous_evidence=True).route is InferenceRoute.DELIBERATE
    digest = product().digest
    failed = report(digest, CheckStatus.UNKNOWN)
    assert (
        policy.choose(
            **kwargs, report=failed, subject_digest=digest, attempted=[InferenceRoute.FAST]
        ).route
        is InferenceRoute.DELIBERATE
    )
    assert (
        policy.choose(
            **kwargs,
            report=failed,
            subject_digest=digest,
            attempted=[InferenceRoute.FAST, InferenceRoute.DELIBERATE],
        ).route
        == "review"
    )
    checked = dict(**kwargs, report=report(digest), subject_digest=digest)
    assert policy.choose(**checked).route is InferenceRoute.ACCEPT
    assert replace(policy, requires_review=True).choose(**checked).route is InferenceRoute.REVIEW
    with pytest.raises(HarnessError):
        policy.choose(
            **kwargs, report=report(digest), subject_digest=canonical_json_digest("stale")
        )


def contributor(name, text="Source text", *, required=False, scope=None, expired=False):
    async def load(_context, _target):
        return ContextContribution(
            scope or target(),
            text,
            NOW - timedelta(hours=1),
            NOW - timedelta(seconds=1) if expired else NOW + timedelta(hours=1),
        )

    return ContextContributor(name, load, required)


@pytest.mark.asyncio
async def test_context_prioritizes_required_data_accounts_for_envelope_and_reports_omissions():
    bundle = await assemble_context(
        [
            contributor("optional", "x" * 10000),
            contributor("required", required=True),
            contributor("expired", expired=True),
        ],
        target=target(),
        context=context(),
        now=NOW,
        max_tokens=200,
    )
    assert bundle.included == ("required",)
    assert bundle.omitted == (("optional", "budget"), ("expired", "stale"))
    assert bundle.token_count <= 200
    assert '"source"' in bundle.text
    with pytest.raises(HarnessError):
        await assemble_context(
            [contributor("required", "x" * 10000, required=True)],
            target=target(),
            context=context(),
            now=NOW,
            max_tokens=200,
        )
    with pytest.raises(HarnessError):
        await assemble_context(
            [contributor("foreign", scope=replace(target(), tenant_id="foreign"))],
            target=target(),
            context=context(),
            now=NOW,
            max_tokens=200,
        )


@pytest.mark.asyncio
async def test_context_errors_timeouts_and_cancellation_do_not_leak_or_silently_succeed():
    async def broken(_context, _target):
        raise ValueError("private credentials")

    async def slow(_context, _target):
        await asyncio.sleep(10)

    bundle = await assemble_context(
        [
            ContextContributor("broken", broken),
            ContextContributor("slow", slow, timeout_seconds=0.001),
        ],
        target=target(),
        context=context(),
        now=NOW,
        max_tokens=200,
    )
    assert bundle.omitted == (("broken", "error"), ("slow", "error"))
    assert "private" not in str(bundle.to_dict())
    task = asyncio.create_task(
        assemble_context(
            [ContextContributor("slow", slow)],
            target=target(),
            context=context(),
            now=NOW,
            max_tokens=200,
        )
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_procedure_completion_and_pack_references_are_checked():
    pack = register_domain()
    procedure = pack.procedures[0]
    value = product()
    procedure.verify_completion(value, report(value.digest))
    with pytest.raises(HarnessError):
        procedure.verify_completion(replace(value, output_version="2"), report(value.digest))
    with pytest.raises(ValueError):
        replace(pack, procedures=(procedure, procedure))
    with pytest.raises(ValueError):
        replace(
            pack,
            procedures=(
                procedure,
                replace(
                    procedure,
                    name="other",
                    output=replace(output(), metadata={"artifact_type": "different"}),
                ),
            ),
        )
    assert pack.capabilities() == (procedure.actions[0].capability,)


def test_existing_pack_loader_requires_trust_for_domain_code(tmp_path):
    path = tmp_path / "agnoclaw-pack.toml"
    path.write_text(
        'name = "diligence"\n[provides]\n'
        'domains = ["tests.test_domain_workflows:register_domain"]\n'
    )
    assert inspect_pack(tmp_path).provides.domains
    with pytest.raises(PackTrustError):
        load_pack(tmp_path)
    loaded = load_pack(tmp_path, trusted=True)
    assert loaded.domains[0].manifest() == register_domain().manifest()
    path.write_text('name = "bad"\n[provides]\ndomains = ["tests.test_domain_core:output"]\n')
    with pytest.raises(PackError):
        load_pack(tmp_path, trusted=True)


@pytest.mark.asyncio
async def test_feedback_attribution_and_explicit_regression_evaluation():
    procedure = register_domain().procedures[0]
    feedback = FeedbackEvent.capture(
        product(),
        context=context(),
        procedure_reference=procedure.reference,
        procedure_digest=procedure.digest,
        kind=FeedbackKind.CORRECTION,
        payload={"value": 121},
    )
    assert feedback.actor_id == "user-1"
    assert feedback.product_digest == product().digest
    case = feedback.regression_case({"source": "value=121"})

    async def correct(_inputs):
        return {"value": 121}

    assert (await evaluate_cases([case], correct)).pass_rate == 1

    async def incorrect(_inputs):
        return {"value": 120}

    assert (await evaluate_cases([case], incorrect)).pass_rate == 0

    async def broken(_inputs):
        raise ValueError("private data")

    summary = await evaluate_cases([case], broken)
    assert summary.results[0].status is CheckStatus.ERROR
    assert "private" not in str(summary.to_dict())
    with pytest.raises(ValueError):
        replace(feedback, kind=FeedbackKind.PREFERENCE).regression_case({})
    with pytest.raises(ValueError):
        await evaluate_cases([], correct)
    with pytest.raises(ValueError):
        await evaluate_cases(
            [EvaluationCase("duplicate", {}, {}), EvaluationCase("duplicate", {}, {})], correct
        )


# Generated by the ship coverage audit.
# Value: protects=feedback always requires authenticated actor attribution;
# fails_when=anonymous feedback capture returns a record;
# why_new=the existing correction journey supplies an authenticated actor; seam=none.
def test_feedback_rejects_anonymous_capture():
    procedure = register_domain().procedures[0]
    with pytest.raises(HarnessError) as rejected:
        FeedbackEvent.capture(
            product(),
            context=replace(context(), user_id=None),
            procedure_reference=procedure.reference,
            procedure_digest=procedure.digest,
            kind=FeedbackKind.CORRECTION,
            payload={"value": 121},
        )
    assert rejected.value.code == "DOMAIN_FEEDBACK_ACTOR_REQUIRED"


# Generated by the ship coverage audit.
# Value: protects=evaluation contains timeouts but propagates caller cancellation;
# fails_when=a runner hangs, survives cancellation, or cancellation becomes an error result;
# why_new=existing evaluation tests cover raised errors but not async interruption; seam=none.
@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["timeout", "cancel"])
async def test_evaluation_timeout_and_cancellation_close_the_runner(interruption):
    entered, finished = asyncio.Event(), asyncio.Event()

    async def waiting(_inputs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    task = asyncio.create_task(
        evaluate_cases(
            [EvaluationCase("bounded", {}, {})],
            waiting,
            timeout_seconds=0.01 if interruption == "timeout" else 10,
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        if interruption == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)
        else:
            summary = await asyncio.wait_for(task, timeout=1)
            assert summary.results[0].status is CheckStatus.ERROR
            assert summary.results[0].actual_digest is None
            assert summary.pass_rate == 0
        assert finished.is_set()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
