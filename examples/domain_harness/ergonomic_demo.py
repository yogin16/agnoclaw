"""Run: python examples/domain_harness/ergonomic_demo.py (offline, temporary SQLite).

The existing DemoHost provides the application's atomic mutation and durable receipt.
Only SDK orchestration changes; business authority and storage ownership stay explicit.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from demo import DemoHost, capability
from pydantic import BaseModel, ConfigDict

from agnoclaw import CapabilityExecutor, CapabilityRegistry
from agnoclaw.domain import (
    Action,
    ChangeSet,
    Check,
    Domain,
    ObjectId,
    ObjectLink,
    ObjectSnapshot,
    OutputSchema,
    Procedure,
    ReviewVerdict,
    TextSource,
    WorkProduct,
    bind_output_capability,
    check_quote,
    locate_quote,
)
from agnoclaw.runtime import (
    AdmissionEnvelope,
    ExecutionContext,
    IdentityAssertion,
    IdentitySource,
    OperationGateway,
    RunSnapshot,
    SQLiteRuntimeStore,
)


class DealFields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revenue_usd: int


async def run(directory: Path) -> dict:
    host = DemoHost(directory / "business.db")
    target = host.current()
    admission = AdmissionEnvelope.resolve(
        IdentityAssertion(
            source=IdentitySource.TRUSTED_HOST,
            tenant_id="demo",
            user_id="reviewer",
            session_id="session-1",
            scopes=("deals:write", "deals:review"),
        )
    )
    context = ExecutionContext.create(
        tenant_id="demo",
        user_id="reviewer",
        session_id="session-1",
        workspace_id=None,
        scopes=admission.identity.scopes,
    )
    source = TextSource(ObjectId("demo", "document", "deck-1").at("1"), "Revenue: USD 120")
    evidence = locate_quote(source, "Revenue: USD 120").evidence
    assert evidence is not None

    def supported(work: WorkProduct) -> bool:
        return (
            work.content["revenue_usd"] == 120
            and check_quote(evidence, source).status.value == "pass"
        )

    extract = Procedure(
        "extract-revenue",
        OutputSchema("deal-fields", DealFields),
        checks=(Check("revenue.support", supported),),
    )
    domain = Domain("diligence", (extract,))
    work = await extract.prepare(
        {"revenue_usd": 120}, target=target, run_id="run-1", context=context, evidence=(evidence,)
    )
    work.require_ready()
    save = bind_output_capability(
        capability("save.output", lambda: host.save, {"type": "object"}),
        extract.output,
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
                "revenue_usd": {"type": "integer"},
                "idempotency_key": {"type": "string"},
            },
            "required": ["expected_revision", "revenue_usd", "idempotency_key"],
        },
    )
    # The authenticated host selects both target arguments and the logical request ID.
    request_id = "host-request-1"
    action = Action(
        mutation,
        checks=(
            Check(
                "proposal.support",
                lambda proposal: (
                    proposal.arguments["revenue_usd"] == work.product.content["revenue_usd"]
                ),
            ),
        ),
    )
    pending = await action.prepare(
        work.product,
        {
            "expected_revision": int(target.revision),
            "revenue_usd": 120,
            "idempotency_key": request_id,
        },
        context=context,
    )
    now = datetime.now(UTC)
    review = pending.request_review(
        summary="Apply extracted revenue",
        required_scope="deals:review",
        expires_at=now + timedelta(hours=1),
    ).decide(ReviewVerdict.APPROVE, context=context, now=now)
    reviews = {
        pending.review_key: review
    }  # Substitute the application's authoritative review store.
    registry = CapabilityRegistry()
    registry.register(save)
    registry.register(mutation)
    store = SQLiteRuntimeStore(directory / "runtime.db")
    store.create_run(
        RunSnapshot(run_id="run-1", tenant_id="demo", user_id="reviewer", session_id="session-1")
    )
    executor = CapabilityExecutor(registry, OperationGateway(store, worker_id="demo"))
    try:
        await executor.execute(
            "save.output@1",
            operation_id="save",
            run_id="run-1",
            attempt_id="attempt-1",
            admission=admission,
            arguments={"content": dict(work.product.content)},
            idempotency_key=work.product.digest,
        )
        dispatch = dict(
            run_id="run-1",
            attempt_id="attempt-1",
            operation_id="apply",
            admission=admission,
            load_current=lambda _: host.current(),
            load_review=lambda value: reviews.get(value.review_key),
            idempotency_key=pending.operation_identity(request_id).key,
        )
        result = await action.execute(executor, pending, **dispatch)
        replay = await action.execute(executor, pending, **dispatch)
    finally:
        await executor.aclose()
        store.close()
    relationship = ObjectLink(source.reference, "evidence-for", target)
    # An independent draft illustrates concurrent edits without changing the saved deal.
    draft = ObjectId("demo", "draft", "draft-1")
    changes = ChangeSet(
        ObjectSnapshot(draft.at("1"), {"revenue": None, "note": "old"}),
        {"revenue": 120, "note": "old"},
    )
    merged = changes.rebase(ObjectSnapshot(draft.at("2"), {"revenue": None, "note": "reviewed"}))
    return {
        "procedure": str(extract.ref),
        "checks_passed": work.ready,
        "committed": result.value,
        "replayed": replay.replayed,
        "link_key": relationship.key,
        "merged_preview": dict(merged.require_clean()),
        "registered_checks": domain.describe()["checks"],
    }


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="agnoclaw-domain-dx-") as temporary:
        print(json.dumps(asyncio.run(run(Path(temporary))), indent=2))
