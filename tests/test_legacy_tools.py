"""Opaque tools= normalization and durable-profile rejection contracts."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from agno.tools.toolkit import Toolkit

from agnoclaw import AgentHarness, HarnessConfig
from agnoclaw.legacy_tools import normalize_legacy_tools
from agnoclaw.runtime import HarnessError
from agnoclaw.runtime.security import thaw_data


def legacy_lookup(*, sku: str) -> str:
    """Look up one item."""
    return sku


class PairToolkit(Toolkit):
    def __init__(self) -> None:
        super().__init__(name="pair")
        self.register(self.read_item)
        self.register(self.write_item)

    def read_item(self, item_id: str) -> str:
        return item_id

    def write_item(self, item_id: str, value: str) -> str:
        return f"{item_id}:{value}"


class CompatibilityAgent:
    def __init__(self, **kwargs) -> None:
        self.system_message = kwargs["system_message"]
        self.session_id = kwargs.get("session_id")
        self.user_id = kwargs.get("user_id")
        self.tools = list(kwargs.get("tools") or [])
        self.db = kwargs.get("db")
        self.learning = kwargs.get("learning")

    async def arun(self, _message, **_kwargs):
        return SimpleNamespace(content="compatibility-ok")


def test_plain_callable_normalizes_to_stable_opaque_non_durable_spec() -> None:
    first = normalize_legacy_tools((legacy_lookup,))[0]
    repeated = normalize_legacy_tools((legacy_lookup,))[0]

    assert first.spec == repeated.spec
    assert first.advertised_name == "legacy_lookup"
    assert first.spec.name.startswith("legacy.legacy_lookup.")
    assert first.spec.trust.value == "opaque_legacy"
    assert first.spec.recovery.value == "live_only"
    assert first.spec.concurrency.value == "serialized"
    assert first.spec.effect_class.value == "non_repeatable"
    assert first.spec.factory is None
    assert thaw_data(first.spec.input_schema) == {
        "type": "object",
        "additionalProperties": True,
    }
    with pytest.raises(HarnessError) as caught:
        first.spec.require_profile("durable")
    assert caught.value.code == "CAPABILITY_NOT_DURABLE"


def test_toolkit_expands_to_bounded_named_opaque_specs() -> None:
    bindings = normalize_legacy_tools((PairToolkit(),))

    assert [binding.advertised_name for binding in bindings] == [
        "read_item",
        "write_item",
    ]
    assert len({binding.spec.digest for binding in bindings}) == 2
    assert all("PairToolkit" in binding.container_type for binding in bindings)


def test_empty_dynamic_toolkit_is_inventoried_before_discovery() -> None:
    toolkit = Toolkit(name="remote_dynamic")

    binding = normalize_legacy_tools((toolkit,), source="dynamic")[0]

    assert binding.advertised_name == "remote_dynamic"
    assert binding.spec.trust.value == "opaque_legacy"
    assert binding.spec.recovery.value == "live_only"


def test_duplicate_names_preserve_compatibility_precedence() -> None:
    def duplicate() -> None:
        return None

    bindings = normalize_legacy_tools((duplicate, duplicate))

    assert [binding.precedence for binding in bindings] == [0, 1]
    assert [binding.shadowed for binding in bindings] == [False, True]


def test_invalid_and_over_budget_raw_tools_fail_at_construction() -> None:

    with pytest.raises(HarnessError) as invalid_error:
        normalize_legacy_tools((object(),))
    assert invalid_error.value.code == "LEGACY_TOOL_INVALID"

    many = []
    for index in range(3):
        tool = lambda: None  # noqa: E731
        tool.__name__ = f"tool_{index}"
        many.append(tool)
    with pytest.raises(HarnessError) as budget_error:
        normalize_legacy_tools(tuple(many), max_tools=2)
    assert budget_error.value.code == "LEGACY_TOOL_BUDGET_EXCEEDED"


@pytest.mark.asyncio
async def test_agent_start_rejects_raw_tools_before_run_creation(tmp_path) -> None:
    runtime_store = MagicMock()
    with patch("agnoclaw.agent.Agent", MagicMock()):
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            harness = AgentHarness(
                model="model",
                provider="custom",
                workspace_dir=tmp_path / "workspace",
                config=HarnessConfig(enable_plugins=False),
                include_default_tools=False,
                tools=[legacy_lookup],
                runtime_store=runtime_store,
            )

    with pytest.raises(HarnessError) as caught:
        await harness.start("use the raw tool")

    assert caught.value.code == "LEGACY_TOOL_DURABLE_UNSUPPORTED"
    assert caught.value.details == {
        "tool_count": 1,
        "tool_names": ("legacy_lookup",),
    }
    runtime_store.create_run.assert_not_called()

    inventory = harness.admin_harness_capabilities()["registry"]
    assert inventory["legacy_compatibility_tools"][0]["advertised_name"] == ("legacy_lookup")
    assert inventory["legacy_compatibility_tools"][0]["trust"] == "opaque_legacy"
    assert inventory["legacy_compatibility_tools"][0]["recovery"] == "live_only"
    assert inventory["legacy_compatibility_tools"][0]["shadowed"] is False


@pytest.mark.asyncio
async def test_direct_arun_retains_serialized_raw_tool_compatibility(tmp_path) -> None:
    with patch("agnoclaw.agent.Agent", CompatibilityAgent):
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            harness = AgentHarness(
                model="model",
                provider="custom",
                workspace_dir=tmp_path / "workspace",
                config=HarnessConfig(enable_plugins=False),
                include_default_tools=False,
                tools=[legacy_lookup],
            )

    result = await harness.arun("compatibility call")

    assert result.content == "compatibility-ok"
    descriptor = next(
        item for item in harness._spec.resources if item.resource_id.endswith(":legacy_lookup")
    )
    assert descriptor.trust.value == "legacy_serialized"
    assert descriptor.recovery.value == "live_only"


@pytest.mark.asyncio
async def test_start_rejects_per_run_raw_tools_before_creation(tmp_path) -> None:
    runtime_store = MagicMock()
    with patch("agnoclaw.agent.Agent", CompatibilityAgent):
        with patch("agnoclaw.agent._make_db", return_value=MagicMock()):
            harness = AgentHarness(
                model="model",
                provider="custom",
                workspace_dir=tmp_path / "workspace",
                config=HarnessConfig(enable_plugins=False),
                include_default_tools=False,
                runtime_store=runtime_store,
            )

    with pytest.raises(HarnessError) as caught:
        await harness.start("use it", tools=[legacy_lookup])

    assert caught.value.code == "LEGACY_TOOL_DURABLE_UNSUPPORTED"
    runtime_store.create_run.assert_not_called()
