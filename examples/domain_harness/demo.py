"""Offline public-API journey: evidence -> output -> review -> committed action.

Run: uv run python examples/domain_harness/demo.py
No model calls, credentials, Clarion database, or external side effects.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import tempfile
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agnoclaw import (
    CapabilityConcurrency,
    CapabilityExecutor,
    CapabilityKind,
    CapabilityLifetime,
    CapabilityRecovery,
    CapabilityRegistry,
    CapabilitySpec,
    CapabilityTrust,
    EffectClass,
)
from agnoclaw.domain import (
    ActionSpec,
    CheckResult,
    CheckStatus,
    ContextContribution,
    ContextContributor,
    DecisionRule,
    DecisionTable,
    FeedbackEvent,
    FeedbackKind,
    InferencePolicy,
    ObjectRef,
    ReviewRequest,
    ReviewVerdict,
    TextSource,
    VerificationReport,
    assemble_context,
    bind_output_capability,
    check_quote,
    evaluate_cases,
    locate_quote,
)
from agnoclaw.packs import load_pack
from agnoclaw.runtime import (
    AdmissionEnvelope,
    ExecutionContext,
    IdentityAssertion,
    IdentitySource,
    OperationGateway,
    RunSnapshot,
    SQLiteRuntimeStore,
)
from agnoclaw.runtime.security import canonical_json_digest, thaw_data


class DemoHost:
    """Example business authority with atomic revision check and idempotency receipt."""

    def __init__(self, path: Path):
        self.path = path
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript("""
                CREATE TABLE deals (id TEXT PRIMARY KEY, revision INTEGER, revenue INTEGER);
                INSERT INTO deals VALUES ('deal-1', 1, NULL);
                CREATE TABLE receipts (key TEXT PRIMARY KEY, arguments TEXT, result TEXT);
                CREATE TABLE outputs (digest TEXT PRIMARY KEY, content TEXT);
            """)

    def current(self) -> ObjectRef:
        with closing(sqlite3.connect(self.path)) as db, db:
            row = db.execute("SELECT revision FROM deals WHERE id = 'deal-1'").fetchone()
        return ObjectRef("demo", "deal", "deal-1", str(row[0]))

    def save(self, product):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "INSERT OR IGNORE INTO outputs VALUES (?, ?)",
                (product.digest, json.dumps(product.to_dict())),
            )
        return {"product_digest": product.digest}

    def apply(self, expected_revision: int, revenue_usd: int, idempotency_key: str):
        arguments = canonical_json_digest([expected_revision, revenue_usd])
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT arguments, result FROM receipts WHERE key = ?", (idempotency_key,)
            ).fetchone()
            if existing:
                if existing[0] != arguments:
                    raise ValueError("idempotency key reused with different arguments")
                return json.loads(existing[1])
            updated = db.execute(
                "UPDATE deals SET revenue = ?, revision = revision + 1 "
                "WHERE id = ? AND revision = ?",
                (revenue_usd, "deal-1", expected_revision),
            )
            if updated.rowcount != 1:
                raise ValueError("stale deal revision")
            result = {"revision": expected_revision + 1, "revenue_usd": revenue_usd}
            db.execute(
                "INSERT INTO receipts VALUES (?, ?, ?)",
                (idempotency_key, arguments, json.dumps(result)),
            )
        return result  # Host commit completed before runtime settlement.


def capability(name, factory, schema):
    return CapabilitySpec(
        name=name,
        version="1",
        kind=CapabilityKind.TOOL,
        effect_class=EffectClass.IDEMPOTENT,
        trust=CapabilityTrust.HOST_MANAGED,
        lifetime=CapabilityLifetime.RUN,
        concurrency=CapabilityConcurrency.ISOLATED,
        recovery=CapabilityRecovery.RECONCILABLE,
        implementation_digest=canonical_json_digest([name, "demo-v1"]),
        input_schema=schema,
        required_scopes=("deals:write",),
        supports_idempotency_key=True,
        factory=factory,
    )


async def run(directory: Path) -> dict:
    host = DemoHost(directory / "business.db")
    target = host.current()
    now = datetime.now(UTC)
    context = ExecutionContext.create(
        tenant_id="demo",
        user_id="reviewer",
        session_id="session-1",
        workspace_id=None,
        scopes=("deals:write", "deals:review"),
    )
    pack = load_pack(Path(__file__).parent, trusted=True).domains[0]
    procedure, invoice_procedure = pack.procedures
    source = TextSource(ObjectRef("demo", "document", "deck-1", "1"), "Revenue:\nUSD 120")
    match = locate_quote(source, "Revenue: USD 120")
    assert match.evidence is not None
    evidence = match.evidence

    async def load_source(_context, _target):
        return ContextContribution(source.reference, source.text, now, now + timedelta(hours=1))

    bundle = await assemble_context(
        [ContextContributor("document", load_source, required=True)],
        target=target,
        context=context,
        now=now,
        max_tokens=500,
    )
    # A model would supply this content through the bound output capability.
    content = {"revenue_usd": 120}
    product = procedure.output.build(
        content, target=target, context=context, run_id="run-1", evidence=(evidence,)
    )
    extracted = re.fullmatch(r"Revenue:\s*USD\s+(\d+)", source.text)
    supported = (
        check_quote(evidence, source).status is CheckStatus.PASS
        and extracted is not None
        and int(extracted[1]) == product.content["revenue_usd"]
    )
    status = CheckStatus.PASS if supported else CheckStatus.FAIL
    completion = VerificationReport(
        product.digest,
        (
            CheckResult(
                "revenue.support",
                "1",
                product.digest,
                status,
                "Fixture amount checked.",
                (evidence.digest,),
            ),
        ),
    )
    procedure.verify_completion(product, completion)
    save = bind_output_capability(
        capability("save.output", lambda: host.save, {"type": "object"}),
        procedure.output,
        target=target,
        context=context,
        run_id="run-1",
        evidence=(evidence,),
    )
    mutation = capability(
        "deal.apply",
        lambda: host.apply,
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "expected_revision": {"type": "integer"},
                "revenue_usd": {"type": "integer", "minimum": 0},
                "idempotency_key": {"type": "string"},
            },
            "required": ["expected_revision", "revenue_usd", "idempotency_key"],
        },
    )
    action = ActionSpec(mutation, ("proposal.support@1",))
    proposal = action.propose(
        product,
        {"expected_revision": 1, "revenue_usd": 120, "idempotency_key": product.digest},
        context=context,
    )
    proposal_report = VerificationReport(
        proposal.digest,
        (
            CheckResult(
                "proposal.support",
                "1",
                proposal.digest,
                status,
                "Fixture proposal checked.",
                (product.digest, evidence.digest),
            ),
        ),
    )
    request = ReviewRequest(
        target,
        proposal.digest,
        proposal_report.digest,
        now + timedelta(hours=1),
        "deals:review",
        "Apply extracted revenue",
        {"revenue_usd": {"before": None, "after": 120}},
    )
    approved = request.decide(ReviewVerdict.APPROVE, context=context, now=now)
    registry = CapabilityRegistry()
    registry.register(save)
    registry.register(mutation)
    store = SQLiteRuntimeStore(directory / "runtime.db")
    store.create_run(
        RunSnapshot(run_id="run-1", tenant_id="demo", user_id="reviewer", session_id="session-1")
    )
    executor = CapabilityExecutor(registry, OperationGateway(store, worker_id="demo"))
    admission = AdmissionEnvelope.resolve(
        IdentityAssertion(
            source=IdentitySource.TRUSTED_HOST,
            tenant_id="demo",
            user_id="reviewer",
            session_id="session-1",
            scopes=context.scopes,
        )
    )
    execution_args = dict(run_id="run-1", attempt_id="attempt-1", admission=admission)
    try:
        await executor.execute(
            "save.output@1",
            operation_id="save",
            arguments={"content": content},
            idempotency_key=product.digest,
            **execution_args,
        )

        def preflight():
            action.preflight(
                proposal,
                context=context,
                current_target=host.current(),
                report=proposal_report,
                review=approved,
                now=datetime.now(UTC),
            )

        first = await executor.execute(
            action.reference,
            operation_id="apply",
            arguments=thaw_data(proposal.arguments),
            idempotency_key=proposal.digest,
            pre_dispatch=preflight,
            **execution_args,
        )
        replay = await executor.execute(
            action.reference,
            operation_id="apply",
            arguments=thaw_data(proposal.arguments),
            idempotency_key=proposal.digest,
            pre_dispatch=preflight,
            **execution_args,
        )
    finally:
        await executor.aclose()
        store.close()

    invoice = invoice_procedure.output.build(
        {"subtotal_cents": 10000, "tax_cents": 500, "total_cents": 10500},
        target=ObjectRef("demo", "invoice", "invoice-1", "1"),
        context=context,
        run_id="run-2",
    )
    table = DecisionTable(
        "invoice-arithmetic",
        "1",
        canonical_json_digest("arithmetic-v1"),
        (
            DecisionRule(
                "balanced",
                "balanced",
                lambda facts: facts["subtotal_cents"] + facts["tax_cents"] == facts["total_cents"],
            ),
            DecisionRule(
                "mismatch",
                "review",
                lambda facts: facts["subtotal_cents"] + facts["tax_cents"] != facts["total_cents"],
            ),
        ),
    )
    decision = table.evaluate(invoice.content)
    invoice_report = VerificationReport(
        invoice.digest,
        (
            CheckResult(
                "invoice.arithmetic",
                "1",
                invoice.digest,
                CheckStatus.PASS if decision.outcome == "balanced" else CheckStatus.FAIL,
                "Integer-cent arithmetic checked.",
            ),
        ),
    )
    invoice_procedure.verify_completion(invoice, invoice_report)
    feedback = FeedbackEvent.capture(
        product,
        context=context,
        procedure_reference=procedure.reference,
        procedure_digest=procedure.digest,
        kind=FeedbackKind.CORRECTION,
        payload={"revenue_usd": 121},
    )

    async def regression(_inputs):
        return {"revenue_usd": 121}

    evaluation = await evaluate_cases(
        [feedback.regression_case({"text": "Revenue: USD 121"})], regression
    )
    return {
        "product": product.digest,
        "context_tokens": bundle.token_count,
        "committed": first.value,
        "replayed": replay.replayed,
        "invoice_decision": decision.outcome,
        "regression_pass_rate": evaluation.pass_rate,
        "default_inference_route": InferencePolicy()
        .choose(required_checks=("revenue.support@1",))
        .route.value,
    }


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="agnoclaw-domain-") as temporary:
        print(json.dumps(asyncio.run(run(Path(temporary))), indent=2))
