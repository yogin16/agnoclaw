"""Two domain contracts, deliberately independent of Clarion and model providers."""

from pydantic import BaseModel, ConfigDict, Field

from agnoclaw.domain import DomainPack, OutputSpec, ProcedureSpec


class DealFields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revenue_usd: int = Field(ge=0)


class InvoiceReconciliation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Integer cents avoid floating-point financial comparison in this fixture.
    subtotal_cents: int = Field(ge=0)
    tax_cents: int = Field(ge=0)
    total_cents: int = Field(ge=0)


def register() -> DomainPack:
    return DomainPack(
        "domain-examples",
        "0.1.0",
        (
            ProcedureSpec(
                "deal-field-extraction",
                "1",
                "deal-field-extraction",
                OutputSpec(
                    "deal-fields", "1", DealFields, {"artifact_type": "deal_field_extraction"}
                ),
                ("revenue.support@1",),
            ),
            ProcedureSpec(
                "invoice-reconciliation",
                "1",
                "invoice-reconciliation",
                OutputSpec(
                    "invoice-reconciliation",
                    "1",
                    InvoiceReconciliation,
                    {"artifact_type": "reconciliation"},
                ),
                ("invoice.arithmetic@1",),
            ),
        ),
    )
