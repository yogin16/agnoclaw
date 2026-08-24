"""Fresh, identity-bound Agno model construction for explicit runtime profiles."""

from __future__ import annotations

import re
import threading
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field

from agno.models.base import Model

from .runtime.errors import HarnessError

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")


@dataclass(frozen=True, slots=True)
class AgnoModelFactory:
    """Create one fresh caller-owned Agno model transport for every harness run.

    ``implementation_digest`` identifies the factory implementation/configuration
    without serializing credentials. The callable must return a new ``Model`` object
    on every invocation; agnoclaw closes the construction-time model and every
    run-owned model it creates.
    """

    model_id: str
    implementation_digest: str
    factory: Callable[[], Model] = field(repr=False, compare=False)
    provider: str | None = None
    _instances: dict[int, weakref.ReferenceType[Model]] = field(
        default_factory=dict,
        init=False,
        repr=False,
        compare=False,
    )
    _instance_lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.model_id, str) or not _IDENTIFIER_RE.fullmatch(self.model_id):
            raise HarnessError(
                code="MODEL_FACTORY_ID_INVALID",
                category="configuration",
                message="AgnoModelFactory.model_id must be a stable provider-safe identifier.",
                retryable=False,
            )
        if self.provider is not None and (
            not isinstance(self.provider, str) or not _IDENTIFIER_RE.fullmatch(self.provider)
        ):
            raise HarnessError(
                code="MODEL_FACTORY_PROVIDER_INVALID",
                category="configuration",
                message="AgnoModelFactory.provider must be a stable provider-safe identifier.",
                retryable=False,
            )
        if not isinstance(self.implementation_digest, str) or not _DIGEST_RE.fullmatch(
            self.implementation_digest
        ):
            raise HarnessError(
                code="MODEL_FACTORY_DIGEST_INVALID",
                category="configuration",
                message="AgnoModelFactory requires a canonical sha256 implementation digest.",
                retryable=False,
            )
        if not callable(self.factory):
            raise HarnessError(
                code="MODEL_FACTORY_CALLABLE_REQUIRED",
                category="configuration",
                message="AgnoModelFactory.factory must be callable.",
                retryable=False,
            )

    @property
    def resolved_name(self) -> str:
        """Return the stable provider/model name used in manifests and diagnostics."""
        return f"{self.provider}:{self.model_id}" if self.provider else self.model_id

    def create(self) -> Model:
        """Create and validate one model without exposing factory exception content."""
        try:
            model = self.factory()
        except HarnessError:
            raise
        except Exception as exc:
            raise HarnessError(
                code="MODEL_FACTORY_CREATION_FAILED",
                category="configuration",
                message="The configured Agno model factory could not create a model.",
                retryable=False,
                details={"exception_type": type(exc).__name__},
            ) from exc
        if not isinstance(model, Model):
            raise HarnessError(
                code="MODEL_FACTORY_RESULT_INVALID",
                category="configuration",
                message="AgnoModelFactory.factory must return an Agno Model instance.",
                retryable=False,
                details={"result_type": type(model).__name__},
            )
        actual_id = str(getattr(model, "id", "") or "")
        if actual_id != self.model_id:
            raise HarnessError(
                code="MODEL_FACTORY_IDENTITY_MISMATCH",
                category="configuration",
                message="The created Agno model does not match the declared model identity.",
                retryable=False,
                details={"declared_model_id": self.model_id, "actual_model_id": actual_id},
            )
        actual_provider = str(getattr(model, "provider", "") or "")
        if self.provider and actual_provider.lower() != self.provider.lower():
            raise HarnessError(
                code="MODEL_FACTORY_IDENTITY_MISMATCH",
                category="configuration",
                message="The created Agno model does not match the declared provider identity.",
                retryable=False,
                details={
                    "declared_provider": self.provider,
                    "actual_provider": actual_provider,
                },
            )
        with self._instance_lock:
            dead = [
                identity
                for identity, reference in self._instances.items()
                if reference() is None
            ]
            for identity in dead:
                self._instances.pop(identity, None)
            identity = id(model)
            previous = self._instances.get(identity)
            if previous is not None and previous() is model:
                raise HarnessError(
                    code="MODEL_FACTORY_SHARED_INSTANCE",
                    category="configuration",
                    message="AgnoModelFactory must return a fresh model instance every time.",
                    retryable=False,
                )
            try:
                self._instances[identity] = weakref.ref(model)
            except TypeError as exc:
                raise HarnessError(
                    code="MODEL_FACTORY_RESULT_UNTRACKABLE",
                    category="configuration",
                    message=(
                        "The created Agno model cannot be tracked for fresh-instance ownership."
                    ),
                    retryable=False,
                    details={"result_type": type(model).__name__},
                ) from exc
        return model


__all__ = ["AgnoModelFactory"]
