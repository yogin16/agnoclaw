"""Contract tests for the centralized Agno compatibility boundary."""

from __future__ import annotations

import inspect

import pytest
from agno.agent import Agent

from agnoclaw.compat import (
    AgnoCompatibilityReport,
    AgnoFeature,
    AgnoLane,
    CapabilityStatus,
    classify_agno_version,
    inspect_agno_compatibility,
    parse_agno_version,
    require_supported_agno,
)
from agnoclaw.runtime import AgnoCapabilityError, AgnoVersionError


@pytest.mark.parametrize(
    ("version", "lane"),
    [
        ("2.6.4", AgnoLane.LEGACY),
        ("2.6.22", AgnoLane.LEGACY),
        ("2.7.4", AgnoLane.STABLE),
        ("2.8.7", AgnoLane.STABLE),
        ("2.9.0", AgnoLane.STABLE),
        ("3.0.0a1", AgnoLane.PREVIEW),
        ("3.0.0", AgnoLane.STABLE_V3),
        ("3.0.1", AgnoLane.STABLE_V3),
        ("3.1.0a1", AgnoLane.PREVIEW),
        ("2.6.3", AgnoLane.UNSUPPORTED),
        ("3.1.0", AgnoLane.UNSUPPORTED),
    ],
)
def test_classify_agno_version(version, lane):
    assert classify_agno_version(version) == lane


def test_parse_agno_version_orders_prerelease_before_stable():
    assert parse_agno_version("3.0.0a1") < parse_agno_version("3.0.0")


def test_parse_agno_version_rejects_unknown_shape():
    with pytest.raises(AgnoVersionError) as exc:
        parse_agno_version("main")
    assert exc.value.code == "AGNO_VERSION_UNSUPPORTED"


def test_installed_supported_report_has_core_contracts():
    report = inspect_agno_compatibility()

    assert report.production_supported or report.preview
    assert report.has(AgnoFeature.LEARNING_MACHINE)
    assert report.has(AgnoFeature.LEARNED_KNOWLEDGE)
    assert report.has(AgnoFeature.LEARNING_EXACT_NAME_INSPECTION)
    assert report.has(AgnoFeature.SESSION_CONTEXT)
    assert report.has(AgnoFeature.LEARNING_ADMIN_CRUD)
    assert report.has(AgnoFeature.MODEL_EVALUATION_SUBJECT)
    assert report.has(AgnoFeature.CONTEXT_PROVIDERS)
    assert report.has(AgnoFeature.CANCEL_CONTINUE)
    assert report.has(AgnoFeature.TOOL_BATCH_CHECKPOINT) is bool(
        "checkpoint" in inspect.signature(Agent.__init__).parameters
        and callable(getattr(Agent, "acancel_run", None))
        and callable(getattr(Agent, "acontinue_run", None))
    )


def test_report_require_raises_actionable_capability_error():
    report = AgnoCompatibilityReport(
        version="2.6.4",
        lane=AgnoLane.LEGACY,
        production_supported=True,
        preview=False,
        capabilities=(
            CapabilityStatus(
                feature=AgnoFeature.FILESYSTEM,
                available=False,
                reason="requires Agno 2.8+",
            ),
        ),
    )

    with pytest.raises(AgnoCapabilityError) as exc:
        report.require(AgnoFeature.FILESYSTEM)

    assert exc.value.code == "AGNO_CAPABILITY_UNAVAILABLE"
    assert exc.value.details == {
        "agno_version": "2.6.4",
        "feature": "filesystem",
        "reason": "requires Agno 2.8+",
    }


def test_require_supported_agno_returns_certified_lane():
    installed = inspect_agno_compatibility()
    report = require_supported_agno(allow_preview=installed.preview)
    assert report.production_supported or report.preview


def test_report_serialization_is_stable():
    report = inspect_agno_compatibility()
    payload = report.to_dict()

    assert payload["supported_spec"] == ">=2.6.4,<3.1"
    assert payload["capabilities"][0]["feature"] == "learning_machine"
