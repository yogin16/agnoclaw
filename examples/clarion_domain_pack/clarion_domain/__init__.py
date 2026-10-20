"""A distributable, offline-importable domain package using public agnoclaw APIs.

The finance rules here are illustrative. Clarion supplies production schemas, policy,
authorization, object reads/writes and outbox handling through its own host adapters.
"""

from importlib.resources import files
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agnoclaw.capabilities import CapabilitySpec
from agnoclaw.domain import (
    Check,
    DecisionRule,
    DecisionTable,
    Domain,
    Entity,
    EvaluationCase,
    EvaluationSuite,
    EventType,
    Finding,
    Hook,
    Interaction,
    Interface,
    Listener,
    MatchPolicy,
    ObjectId,
    Procedure,
    Relation,
    ResolutionPolicy,
    Resources,
    Schema,
    SkillBinding,
    Trigger,
    Watcher,
    WatcherResult,
    Workflow,
    WorkflowStep,
    WorkProduct,
)
from agnoclaw.runtime.context import ExecutionContext
from agnoclaw.runtime.security import canonical_json_digest


class CompanyData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    legal_id: str
    name: str


class DealData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str


class MetricData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount_minor: int
    currency: Literal["USD", "EUR"]
    period: str = Field(min_length=1)


class SavedMetricData(MetricData):
    company_id: str
    revision: str


class ReviewAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    choice: Literal["retain", "revise"]


company = Entity("company", Schema("company-data", CompanyData), label_fields=("name",))
deal = Entity("deal", Schema("deal-data", DealData), label_fields=("name",))
deal_company = Relation("deal.company", deal, company, max_targets=1)
metric = Schema("company-metric", MetricData, metadata={"artifact_type": "company_metric"})
skill = SkillBinding("company-metric", metric, resource="skills/company-metric/SKILL.md")
resources = Resources("company-metric-skills", (skill.resource,))


def nonnegative(work: WorkProduct) -> bool:
    return work.content["amount_minor"] >= 0


extract = Procedure(
    "extract-company-metric",
    metric,
    skill=skill,
    checks=(Check("metric.nonnegative", nonnegative),),
)


def review_large(facts):
    return facts["amount_minor"] >= 10_000_000


def routine(facts):
    return 0 <= facts["amount_minor"] < 10_000_000


review_policy = DecisionTable(
    "metric-review",
    "1",
    canonical_json_digest({"build": "example-rules-v1"}),
    (DecisionRule("large", "review", review_large), DecisionRule("routine", "normal", routine)),
)
compare_metric = Interaction(
    "metric-comparison", Schema("metric-review-answer", ReviewAnswer), affordance="comparison"
)
metric_saved = EventType("company.metric.saved", Schema("saved-metric", SavedMetricData))


def detect_large_change(event, context):
    if event.payload["amount_minor"] < 10_000_000:
        return WatcherResult(1, suppressed=1, source_health={event.source: "available"})
    target = ObjectId(context.tenant_id, "company", event.payload["company_id"]).at(
        event.payload["revision"]
    )
    return WatcherResult(
        1,
        (Finding(event.event_id, target, {"reason": "Review a large metric"}),),
        source_health={event.source: "available"},
    )


watcher = Watcher(
    "large-metric", metric_saved, detect_large_change, implementation_id="detector-v1"
)
on_metric_saved = Trigger("inspect-saved-metric", metric_saved, watcher)
preferred_source = ResolutionPolicy("metric-source", ("filing", "board-report", "pitch-deck"))


def same_legal_id(a, b):
    return 1.0 if a.data["legal_id"] == b.data["legal_id"] else None


company_matching = MatchPolicy("company-legal-id", same_legal_id, implementation_id="matcher-v1")


def positive_input(values, context):
    return values["amount_minor"] >= 0


metric_guard = Hook("metric-input", metric, positive_input, implementation_id="guard-v1")


async def extraction_fixture(inputs):
    context = ExecutionContext.create(
        tenant_id="fixtures", user_id="test", session_id="test", workspace_id=None
    )
    prepared = await extract.prepare(
        inputs,
        target=ObjectId("fixtures", "company", "acme").at("1"),
        run_id="fixture",
        context=context,
    )
    return {"ready": prepared.ready}


fixtures = EvaluationSuite(
    "metric-extraction",
    (
        EvaluationCase(
            "reported",
            {"amount_minor": 12000, "currency": "USD", "period": "2025"},
            {"ready": True},
        ),
        EvaluationCase(
            "negative", {"amount_minor": -1, "currency": "USD", "period": "2025"}, {"ready": False}
        ),
    ),
    extraction_fixture,
    implementation_id="fixtures-v1",
    subjects=(extract, review_policy),
)

pack = Domain(
    "clarion-company-metrics",
    (extract,),
    components=(
        deal_company,
        resources,
        review_policy,
        compare_metric,
        on_metric_saved,
        preferred_source,
        company_matching,
        metric_guard,
        fixtures,
    ),
    aliases={"builtin:company_metric": metric},
)


def register() -> Domain:
    """Existing trusted pack-loader entry point; no models, I/O or workers start."""
    return pack


def for_host(validate_metric: CapabilitySpec, listener: Listener) -> Domain:
    """Bind an existing host capability and its committed-event consumer once."""
    contract = Interface("validate-company-metric", metric, metric)
    flow = Workflow("validate-metric", (WorkflowStep("validate", contract.bind(validate_metric)),))
    return Domain("clarion-host", includes=(pack,), components=(flow, listener))


class InstalledResources:
    """A wheel resource provider; source-checkout demos use LocalResources instead."""

    def read(self, path: str) -> bytes:
        from agnoclaw.domain.resources import resource_path

        return files(__package__).joinpath("resources", resource_path(path)).read_bytes()
