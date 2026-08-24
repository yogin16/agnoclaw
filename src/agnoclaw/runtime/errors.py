"""Typed runtime errors for the harness core."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class HarnessError(Exception):
    """Stable error shape for public runtime operations."""

    code: str
    category: str
    message: str
    retryable: bool
    details: dict[str, Any] | None = None

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"[{self.code}] {self.message}"


class AgnoConfigError(HarnessError):
    """Raised when model/provider configuration is invalid."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(
            code="AGNO_CONFIG_ERROR",
            category="config",
            message=message,
            retryable=False,
            details=details,
        )


class AgnoAuthError(HarnessError):
    """Raised when model/provider authentication is missing or invalid."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(
            code="AGNO_AUTH_ERROR",
            category="auth",
            message=message,
            retryable=False,
            details=details,
        )


class AgnoVersionError(HarnessError):
    """Raised for an unrecognized or uncertified Agno runtime version."""

    def __init__(self, *, version: str, reason: str):
        super().__init__(
            code="AGNO_VERSION_UNSUPPORTED",
            category="compatibility",
            message=f"Agno {version} is unsupported: {reason}.",
            retryable=False,
            details={"agno_version": version, "reason": reason},
        )


class AgnoCapabilityError(HarnessError):
    """Raised when code requests an unavailable version-sensitive capability."""

    def __init__(self, *, feature: str, version: str, reason: str):
        super().__init__(
            code="AGNO_CAPABILITY_UNAVAILABLE",
            category="compatibility",
            message=f"Agno {version} does not provide '{feature}': {reason}.",
            retryable=False,
            details={
                "agno_version": version,
                "feature": feature,
                "reason": reason,
            },
        )


class ModelProviderDependencyError(HarnessError):
    """Raised before Agno imports a model provider SDK that is not installed."""

    def __init__(self, *, provider: str, package: str, install_extra: str):
        super().__init__(
            code="MODEL_PROVIDER_DEPENDENCY_MISSING",
            category="config",
            message=(
                f"Model provider '{provider}' requires package '{package}'. "
                f"Install it with: pip install '{install_extra}'."
            ),
            retryable=False,
            details={
                "provider": provider,
                "package": package,
                "install_extra": install_extra,
            },
        )


def from_exception(
    exc: Exception,
    *,
    code: str = "INTERNAL_UNEXPECTED",
    category: str = "internal",
    retryable: bool = False,
    details: dict[str, Any] | None = None,
) -> HarnessError:
    """Convert an arbitrary exception into HarnessError."""
    if isinstance(exc, HarnessError):
        return exc

    payload = dict(details or {})
    payload.setdefault("exception_type", exc.__class__.__name__)
    return HarnessError(
        code=code,
        category=category,
        message=str(exc),
        retryable=retryable,
        details=payload,
    )
