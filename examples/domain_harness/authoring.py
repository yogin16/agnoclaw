"""Copyable domain definitions: no manually duplicated check-reference strings."""

from pydantic import BaseModel, ConfigDict, Field

from agnoclaw.domain import Check, Domain, OutputSchema, Procedure, WorkProduct


class Invoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subtotal_cents: int = Field(ge=0)
    tax_cents: int = Field(ge=0)
    total_cents: int = Field(ge=0)


def amounts_balance(product: WorkProduct) -> bool:
    values = product.content
    return values["subtotal_cents"] + values["tax_cents"] == values["total_cents"]


invoice = Procedure(
    "reconcile-invoice",
    OutputSchema("invoice-result", Invoice),
    checks=(Check("invoice.arithmetic", amounts_balance),),
)
accounting = Domain("accounting", (invoice,))


def register():
    """Optional existing pack-loader entry point; declarations do not start execution."""
    return accounting.pack
