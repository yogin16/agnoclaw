"""Start here: typed calls and offline examples, with no host infrastructure."""

import asyncio

from pydantic import Field

from agnoclaw.domain import Model, Pack

pack = Pack("clarion")


class Metric(Model):
    amount_minor: int = Field(ge=0)
    currency: str
    period: str = Field(min_length=1)


@pack.procedure
def read_metric(text: str) -> Metric:
    """Parse a structured source; a model-backed extractor can use the same signature."""
    return Metric.model_validate_json(text)


@read_metric.check
def supported_currency(metric: Metric) -> bool:
    return metric.currency in {"USD", "EUR"}


SOURCE = '{"amount_minor": 12000, "currency": "USD", "period": "2026-Q3"}'
read_metric.example(SOURCE, expected=Metric(amount_minor=12000, currency="USD", period="2026-Q3"))


def register() -> Pack:
    """Optional entry for the existing trusted directory pack loader."""
    return pack


async def main() -> None:
    await pack.test()
    metric = await read_metric(SOURCE)
    print(metric.model_dump_json())


if __name__ == "__main__":
    asyncio.run(main())
