"""Contracts preventing hosted test lanes from spending LLM tokens."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._ci_safety import CI_NO_LIVE_LLM_ENV, ci_llm_safety_violations

ROOT = Path(__file__).resolve().parents[1]


def test_ci_llm_safety_detects_credentials_and_live_opt_ins_without_values() -> None:
    secret = "do-not-print-this-value"
    violations = ci_llm_safety_violations(
        {
            "ANTHROPIC_API_KEY": secret,
            "OPENAI_API_TOKEN": secret,
            "AWS_ACCESS_KEY_ID": secret,
            "AGNOCLAW_TEST_PROVIDER": "ollama",
            "EMPTY_API_KEY": "",
            "UNRELATED": secret,
        }
    )

    assert violations == (
        "AGNOCLAW_TEST_PROVIDER",
        "ANTHROPIC_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "OPENAI_API_TOKEN",
    )
    assert secret not in repr(violations)


def test_hosted_test_workflows_enable_the_no_live_llm_guard() -> None:
    for relative_path in (".github/workflows/ci.yml", ".github/workflows/publish.yml"):
        workflow = (ROOT / relative_path).read_text(encoding="utf-8")
        assert f'{CI_NO_LIVE_LLM_ENV}: "1"' in workflow


def test_hosted_session_guard_rejects_credentials_without_exposing_values(
    monkeypatch,
) -> None:
    from tests.conftest import pytest_sessionstart

    secret = "do-not-print-this-value"
    monkeypatch.setenv(CI_NO_LIVE_LLM_ENV, "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)

    with pytest.raises(pytest.UsageError) as raised:
        pytest_sessionstart(None)

    assert "ANTHROPIC_API_KEY" in str(raised.value)
    assert secret not in str(raised.value)

def test_live_model_contracts_are_explicitly_marked() -> None:
    module_contracts = (
        "tests/test_integration.py",
        "tests/test_ollama_harness_integration.py",
        "tests/test_learning_benefit_live.py",
    )
    for relative_path in module_contracts:
        content = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "pytest.mark.live_model" in content

    continuity = (ROOT / "tests/test_long_run_continuity_probe.py").read_text(
        encoding="utf-8"
    )
    assert "@pytest.mark.live_model" in continuity
