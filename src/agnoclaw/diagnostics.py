"""Offline diagnostics, error guidance, and redacted support bundles."""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import sys
import tempfile
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any

DIAGNOSTICS_SCHEMA_VERSION = "1.0"

_ERROR_GUIDANCE: dict[str, tuple[str, str, str]] = {
    "AGNO_CONFIG_ERROR": (
        "Model configuration is invalid",
        "The selected provider, model, or configuration value could not be resolved.",
        "Run `agnoclaw doctor`, then check `.agnoclaw.toml`, AGNOCLAW_* settings, "
        "and the provider/model pair.",
    ),
    "AGNO_AUTH_ERROR": (
        "Provider authentication failed",
        "The provider rejected or could not find its credential.",
        "Set the provider SDK's documented credential environment variable and retry; "
        "never put the secret in a CLI argument.",
    ),
    "AGNO_VERSION_UNSUPPORTED": (
        "Installed Agno version is outside the certified lane",
        "The installed Agno API is older, newer, or a preview not supported by this release.",
        "Install a version in the range reported by `agnoclaw doctor` or use the "
        "explicitly quarantined preview lane.",
    ),
    "MODEL_PROVIDER_DEPENDENCY_MISSING": (
        "Provider dependency is not installed",
        "Core agnoclaw is provider-neutral and the selected provider SDK is an optional extra.",
        "Install the matching extra, for example `pip install 'agnoclaw[anthropic]'`, "
        "then rerun `agnoclaw doctor`.",
    ),
    "RUNTIME_PROFILE_CONFLICT": (
        "Runtime profiles conflict",
        "Two configuration sources selected incompatible runtime profiles.",
        "Choose the profile once, either in HarnessConfig or the AgentHarness/profile "
        "environment setting.",
    ),
    "GUARDRAIL_DENIED": (
        "A guardrail denied the operation",
        "The request crossed a configured path, network, or command safety boundary.",
        "Inspect the violation code, narrow the request, or deliberately change the "
        "host policy; do not bypass it from untrusted input.",
    ),
    "PERMISSION_DENIED": (
        "Permission policy denied the operation",
        "The active permission mode or approval policy did not authorize the requested effect.",
        "Use a permitted operation or have the trusted host approve the exact capability request.",
    ),
}

_PREFIX_GUIDANCE: tuple[tuple[str, tuple[str, str, str]], ...] = (
    (
        "RUNTIME_",
        (
            "Durable runtime operation could not proceed",
            "Admission, ownership, lifecycle state, store availability, or recovery "
            "evidence rejected the operation.",
            "Inspect the run with `agnoclaw inspect run`, preserve the original "
            "idempotency key, and reconcile ambiguous effects before retrying.",
        ),
    ),
    (
        "CAPABILITY_",
        (
            "Capability execution was rejected",
            "The capability declaration, arguments, policy, approval, lifetime, or "
            "settlement evidence was invalid.",
            "Inspect the registered CapabilitySpec and retry only after the exact "
            "declaration or approval issue is corrected.",
        ),
    ),
    (
        "CONTEXT_",
        (
            "Context continuity could not proceed safely",
            "A budget, archive, scope, lock, compaction, or rehydration invariant failed.",
            "Preserve the session artifacts, inspect the exact code, and avoid replaying "
            "a turn after tool activity unless the runtime marks it safe.",
        ),
    ),
    (
        "LEARNING_",
        (
            "Learning policy or evidence rejected the change",
            "The candidate lacked scope, consent, evaluation, promotion, rollback, or "
            "reconciliation evidence.",
            "Review the candidate in the learning administration API and supply the "
            "missing bounded evidence instead of writing directly.",
        ),
    ),
    (
        "MIGRATION_",
        (
            "Migration precondition or verification failed",
            "The source, plan digest, writer fence, target evidence, or rollback boundary changed.",
            "Run the matching `migrate 0.12 ... --json` check/verify command and follow "
            "its next_command without editing the plan.",
        ),
    ),
    (
        "SCHEDULER_",
        (
            "Scheduled work could not be claimed or settled",
            "A schedule, lease, fence, retry, overlap, or worker ownership rule rejected "
            "the attempt.",
            "Inspect the job and run history, keep one single-host SQLite worker, and "
            "retry only the same deterministic occurrence.",
        ),
    ),
    (
        "NETWORK_",
        (
            "Network policy blocked the destination",
            "The URL, resolved address, redirect, or transport violated the configured "
            "network boundary.",
            "Use an allowed HTTPS destination or update the trusted host allowlist; "
            "never allow private targets from model-provided URLs.",
        ),
    ),
)


def explain_error_code(code: str) -> dict[str, Any]:
    """Return content-free offline guidance for one stable error code."""
    normalized = code.strip().upper()
    guidance = _ERROR_GUIDANCE.get(normalized)
    if guidance is None:
        guidance = next(
            (value for prefix, value in _PREFIX_GUIDANCE if normalized.startswith(prefix)),
            None,
        )
    found = guidance is not None
    title, cause, fix = guidance or (
        "Unknown error code",
        "This installed release does not have offline guidance for the supplied code.",
        "Confirm the exact code, run `agnoclaw doctor --json`, and include a redacted "
        "support bundle when opening an issue.",
    )
    return {
        "schema_version": DIAGNOSTICS_SCHEMA_VERSION,
        "code": normalized,
        "found": found,
        "title": title,
        "cause": cause,
        "fix": fix,
        "docs": "https://github.com/yogin16/agnoclaw/blob/main/docs/cli.md",
    }


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _distribution_version(name: str) -> str | None:
    try:
        return package_version(name)
    except PackageNotFoundError:
        return None


def collect_diagnostics(*, workspace: str | Path | None = None) -> dict[str, Any]:
    """Collect a bounded offline health report without returning paths or secrets."""
    from agnoclaw import __version__
    from agnoclaw.compat import STABLE_AGNO_SPEC, inspect_agno_compatibility
    from agnoclaw.config import get_config

    checks: list[dict[str, Any]] = []

    def add(check_id: str, status: str, summary: str, fix: str | None = None) -> None:
        item: dict[str, Any] = {"id": check_id, "status": status, "summary": summary}
        if fix:
            item["fix"] = fix
        checks.append(item)

    supported_python = (3, 11) <= sys.version_info[:2] < (3, 15)
    add(
        "python",
        "pass" if supported_python else "error",
        (
            f"Python {platform.python_version()} is "
            f"{'supported' if supported_python else 'unsupported'}."
        ),
        None if supported_python else "Install Python 3.11, 3.12, 3.13, or 3.14.",
    )

    installed_version = _distribution_version("agnoclaw")
    version_matches = installed_version in {None, __version__}
    add(
        "package",
        "pass" if version_matches else "error",
        (
            f"agnoclaw {__version__} is loaded from the "
            f"{'source tree' if installed_version is None else 'installed distribution'}."
        ),
        None
        if version_matches
        else "Reinstall the exact wheel so package metadata and imports agree.",
    )

    agno_payload: dict[str, Any]
    try:
        report = inspect_agno_compatibility()
        agno_payload = {
            "version": report.version,
            "lane": report.lane.value,
            "production_supported": report.production_supported,
            "supported_spec": STABLE_AGNO_SPEC,
        }
        add(
            "agno",
            "pass" if report.production_supported else "warn",
            f"Agno {report.version} is in the {report.lane.value} lane.",
            None
            if report.production_supported
            else f"Use Agno {STABLE_AGNO_SPEC} for the supported release lane.",
        )
    except Exception as exc:
        agno_payload = {"available": False, "error_type": type(exc).__name__}
        add(
            "agno", "error", "Agno compatibility inspection failed.", "Reinstall agnoclaw and Agno."
        )

    profile: str | None = None
    permission_mode: str | None = None
    network_enabled: bool | None = None
    configured_workspace: Path | None = None
    try:
        config = get_config()
        profile = str(config.profile)
        permission_mode = str(config.permission_mode)
        network_enabled = bool(config.network_enabled)
        configured_workspace = Path(config.workspace_dir).expanduser()
        add("configuration", "pass", f"Configuration loaded for the {profile} profile.")
    except Exception as exc:
        add(
            "configuration",
            "error",
            f"Configuration could not be loaded ({type(exc).__name__}).",
            "Check `.agnoclaw.toml` and AGNOCLAW_* values against the configuration reference.",
        )

    workspace_path = Path(workspace).expanduser() if workspace is not None else configured_workspace
    workspace_exists = bool(workspace_path is not None and workspace_path.is_dir())
    workspace_initialized = bool(
        workspace_exists and workspace_path is not None and (workspace_path / "AGENTS.md").is_file()
    )
    add(
        "workspace",
        "pass" if workspace_initialized else "warn",
        "Workspace is initialized." if workspace_initialized else "Workspace is not initialized.",
        None if workspace_initialized else "Run `agnoclaw workspace init --workspace PATH`.",
    )

    extras = {
        "mcp": _module_available("mcp"),
        "otel": _module_available("opentelemetry.sdk"),
        "postgres": _module_available("psycopg"),
        "scheduler": _module_available("croniter"),
        "tui": _module_available("textual"),
    }
    errors = sum(item["status"] == "error" for item in checks)
    warnings = sum(item["status"] == "warn" for item in checks)
    return {
        "schema_version": DIAGNOSTICS_SCHEMA_VERSION,
        "status": "error" if errors else "warn" if warnings else "pass",
        "offline": True,
        "redacted": True,
        "checks": checks,
        "summary": {
            "passed": len(checks) - errors - warnings,
            "warnings": warnings,
            "errors": errors,
        },
        "runtime": {
            "agnoclaw_version": __version__,
            "python_version": platform.python_version(),
            "platform": platform.system().lower(),
            "architecture": platform.machine().lower(),
            "agno": agno_payload,
        },
        "configuration": {
            "profile": profile,
            "permission_mode": permission_mode,
            "network_enabled": network_enabled,
            "workspace_exists": workspace_exists,
            "workspace_initialized": workspace_initialized,
        },
        "optional_extras": extras,
    }


def write_support_bundle(
    output: str | Path,
    *,
    workspace: str | Path | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Atomically write a mode-0600 content-free support bundle."""
    target = Path(output).expanduser().resolve(strict=False)
    if target.exists() and not overwrite:
        raise FileExistsError("support bundle already exists; pass --overwrite to replace it")
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": DIAGNOSTICS_SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "redacted": True,
        "contains": ["versions", "compatibility", "configuration-shape", "offline-checks"],
        "excludes": [
            "credentials",
            "environment-values",
            "paths",
            "prompts",
            "outputs",
            "tool-arguments",
        ],
        "diagnostics": collect_diagnostics(workspace=workspace),
    }
    descriptor, temporary_name = tempfile.mkstemp(prefix=".agnoclaw-support-", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return payload


__all__ = [
    "DIAGNOSTICS_SCHEMA_VERSION",
    "collect_diagnostics",
    "explain_error_code",
    "write_support_bundle",
]
