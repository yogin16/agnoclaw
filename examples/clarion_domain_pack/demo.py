"""Run: python examples/clarion_domain_pack/demo.py (no credentials or model calls)."""

import asyncio
import json
import tempfile
from pathlib import Path

from clarion_domain import extract, metric, metric_saved, on_metric_saved, pack, resources, watcher

from agnoclaw.domain import LocalResources, ObjectId, inspect_domain_manifest
from agnoclaw.runtime.context import ExecutionContext


async def main():
    compiled = pack.compile()
    inspect_domain_manifest(compiled.manifest())
    context = ExecutionContext.create(
        tenant_id="demo", user_id="developer", session_id="demo", workspace_id=None
    )
    values = {"amount_minor": 12000, "currency": "USD", "period": "2025"}
    async with compiled.extensions(tenant_id="demo") as extensions:
        checked_input = await extensions.apply(metric, values, context=context)
        work = await compiled.use(extract).prepare(
            checked_input,
            target=ObjectId("demo", "company", "acme").at("1"),
            run_id="example",
            context=context,
        )
        work.require_ready()
    snapshot = resources.capture(LocalResources(Path(__file__).parent), tenant_id="demo")
    with tempfile.TemporaryDirectory() as directory:
        snapshot.materialize(
            Path(directory).resolve() / "workspace",
            tenant_id="demo",
            expected_digest=snapshot.digest,
        )
    report = await compiled.evaluate(workspace_digest=snapshot.digest)
    report.require_current(compiled, workspace_digest=snapshot.digest)
    # Simulated committed host event; this example does not write a business database.
    event = metric_saved.event(
        {**values, "company_id": "acme", "revision": "2"},
        source="example-host",
        event_id="saved-1",
        tenant_id="demo",
    )
    request = on_metric_saved.plan(event, context=context)
    findings = await watcher.run(event, context=context)
    print(
        json.dumps(
            {
                "definitions": len(compiled.definitions),
                "checks_passed": work.ready,
                "evaluations_passed": report.passed,
                "trigger_planned": request is not None,
                "watcher_suppressed": findings.suppressed,
                "manifest_digest": compiled.digest,
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
