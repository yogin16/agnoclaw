"""Fail-closed safety checks for hosted test environments."""

from __future__ import annotations

from collections.abc import Mapping

CI_NO_LIVE_LLM_ENV = "AGNOCLAW_CI_NO_LIVE_LLM"

_LIVE_MODEL_OPT_INS = frozenset(
    {
        "AGNOCLAW_RUN_LIVE_CONTINUITY",
        "AGNOCLAW_TEST_MODEL",
        "AGNOCLAW_TEST_PROVIDER",
    }
)
_CLOUD_CREDENTIALS = frozenset(
    {
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AZURE_CLIENT_SECRET",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "HF_TOKEN",
        "HUGGING_FACE_HUB_TOKEN",
    }
)


def ci_llm_safety_violations(environ: Mapping[str, str]) -> tuple[str, ...]:
    """Return credential/live-model variable names without exposing their values."""
    violations = {
        name
        for name, value in environ.items()
        if value
        and (
            name in _LIVE_MODEL_OPT_INS
            or name in _CLOUD_CREDENTIALS
            or name.endswith("_API_KEY")
            or name.endswith("_API_TOKEN")
        )
    }
    return tuple(sorted(violations))
