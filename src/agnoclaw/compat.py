"""Central Agno version and capability compatibility boundary.

Product code should ask this module about version-sensitive upstream behavior rather
than growing ad-hoc version parsing and import probes throughout the harness.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import re
from dataclasses import dataclass
from enum import StrEnum
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from typing import Any

from .runtime.errors import AgnoCapabilityError, AgnoVersionError

MIN_AGNO_VERSION = "2.6.4"
PRIMARY_AGNO_VERSION = "3.0.1"
MAX_STABLE_AGNO_VERSION = "3.1.0"
STABLE_AGNO_SPEC = ">=2.6.4,<3.1"

_VERSION_PATTERN = re.compile(
    r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?:(?P<phase>a|b|rc)(?P<phase_number>\d+))?$"
)
_PHASE_ORDER = {"a": 0, "b": 1, "rc": 2, None: 3}


@dataclass(frozen=True, order=True)
class ParsedVersion:
    """Small PEP-440 subset sufficient for Agno's released version scheme."""

    major: int
    minor: int
    patch: int
    phase_order: int = 3
    phase_number: int = 0

    @property
    def prerelease(self) -> bool:
        return self.phase_order < 3


class AgnoLane(StrEnum):
    LEGACY = "legacy-2.6"
    STABLE = "stable-2.x"
    STABLE_V3 = "stable-3.x"
    PREVIEW = "preview-3"
    UNSUPPORTED = "unsupported"


class AgnoFeature(StrEnum):
    LEARNING_MACHINE = "learning_machine"
    LEARNED_KNOWLEDGE = "learned_knowledge"
    LEARNING_EXACT_NAME_INSPECTION = "learning_exact_name_inspection"
    SESSION_CONTEXT = "session_context"
    LEARNING_ADMIN_CRUD = "learning_admin_crud"
    MODEL_EVALUATION_SUBJECT = "model_evaluation_subject"
    CONTEXT_PROVIDERS = "context_providers"
    CANCEL_CONTINUE = "cancel_continue"
    TOOL_BATCH_CHECKPOINT = "tool_batch_checkpoint"
    AGENTOS = "agentos"
    EVALUATION_ENVIRONMENTS = "evaluation_environments"
    FILESYSTEM = "filesystem"
    V3_NORMALIZED_RUN_STORAGE = "v3_normalized_run_storage"
    V3_JOB_QUEUE = "v3_job_queue"
    V3_EVENT_STREAMS = "v3_event_streams"


@dataclass(frozen=True)
class CapabilityStatus:
    feature: AgnoFeature
    available: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature": self.feature.value,
            "available": self.available,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class AgnoCompatibilityReport:
    version: str
    lane: AgnoLane
    production_supported: bool
    preview: bool
    capabilities: tuple[CapabilityStatus, ...]

    def has(self, feature: AgnoFeature | str) -> bool:
        normalized = AgnoFeature(feature)
        return any(item.feature == normalized and item.available for item in self.capabilities)

    def require(self, feature: AgnoFeature | str) -> None:
        normalized = AgnoFeature(feature)
        if self.has(normalized):
            return
        status = next(
            (item for item in self.capabilities if item.feature == normalized),
            None,
        )
        raise AgnoCapabilityError(
            feature=normalized.value,
            version=self.version,
            reason=status.reason if status is not None else "capability was not probed",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "agno_version": self.version,
            "lane": self.lane.value,
            "production_supported": self.production_supported,
            "preview": self.preview,
            "supported_spec": STABLE_AGNO_SPEC,
            "capabilities": [item.to_dict() for item in self.capabilities],
        }


def parse_agno_version(raw: str) -> ParsedVersion:
    match = _VERSION_PATTERN.match(raw.strip())
    if match is None:
        raise AgnoVersionError(
            version=raw,
            reason="version is not in a recognized major.minor.patch release form",
        )
    phase = match.group("phase")
    return ParsedVersion(
        major=int(match.group("major")),
        minor=int(match.group("minor")),
        patch=int(match.group("patch")),
        phase_order=_PHASE_ORDER[phase],
        phase_number=int(match.group("phase_number") or 0),
    )


def classify_agno_version(raw: str) -> AgnoLane:
    parsed = parse_agno_version(raw)
    minimum = parse_agno_version(MIN_AGNO_VERSION)
    maximum = parse_agno_version(MAX_STABLE_AGNO_VERSION)
    if minimum <= parsed < maximum:
        if parsed.prerelease:
            # PEP 440 phase ordering places prereleases inside the stable
            # window; they remain preview builds, never production-supported.
            return AgnoLane.PREVIEW
        if parsed.major == 2:
            if parsed.minor == 6:
                return AgnoLane.LEGACY
            return AgnoLane.STABLE
        if parsed.major == 3:
            return AgnoLane.STABLE_V3
    return AgnoLane.UNSUPPORTED


def installed_agno_version() -> str:
    try:
        return package_version("agno")
    except PackageNotFoundError as exc:
        raise AgnoVersionError(
            version="not-installed",
            reason="install agnoclaw with its required Agno dependency",
        ) from exc


def _module_available(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _symbol_available(module_name: str, symbol: str) -> bool:
    if not _module_available(module_name):
        return False
    try:
        module = importlib.import_module(module_name)
    except Exception:
        return False
    return getattr(module, symbol, None) is not None


def _method_available(module_name: str, symbol: str, methods: tuple[str, ...]) -> bool:
    if not _symbol_available(module_name, symbol):
        return False
    try:
        target = getattr(importlib.import_module(module_name), symbol)
    except Exception:
        return False
    return all(callable(getattr(target, method, None)) for method in methods)


def _parameter_available(
    module_name: str,
    symbol: str,
    method: str,
    parameter: str,
) -> bool:
    """Return whether one importable callable exposes a named parameter."""
    if not _symbol_available(module_name, symbol):
        return False
    try:
        target = getattr(importlib.import_module(module_name), symbol)
        callable_target = target if method == "__init__" else getattr(target, method)
        return parameter in inspect.signature(callable_target).parameters
    except (AttributeError, TypeError, ValueError):
        return False


def _status(feature: AgnoFeature, available: bool, reason: str) -> CapabilityStatus:
    return CapabilityStatus(feature=feature, available=available, reason=reason)


def inspect_agno_compatibility() -> AgnoCompatibilityReport:
    """Inspect the actually installed Agno runtime and its importable contracts."""
    resolved_version = installed_agno_version()
    parsed = parse_agno_version(resolved_version)
    lane = classify_agno_version(resolved_version)

    learning_machine = _symbol_available("agno.learn", "LearningMachine")
    learned_knowledge = _symbol_available("agno.learn.config", "LearnedKnowledgeConfig")
    learning_exact_name_inspection = _method_available(
        "agno.vectordb.base",
        "VectorDb",
        ("name_exists",),
    )
    session_context = _symbol_available("agno.learn.config", "SessionContextConfig")
    learning_admin_crud = all(
        (
            _method_available(
                "agno.learn.stores.user_profile",
                "UserProfileStore",
                ("get", "aget", "save", "asave", "delete", "adelete"),
            ),
            _method_available(
                "agno.learn.stores.user_memory",
                "UserMemoryStore",
                ("get", "aget", "save", "asave", "delete", "adelete"),
            ),
            _method_available(
                "agno.learn.stores.session_context",
                "SessionContextStore",
                ("get", "aget", "save", "asave", "delete", "adelete"),
            ),
            _method_available(
                "agno.db.base",
                "BaseDb",
                ("get_learning", "upsert_learning", "delete_learning"),
            ),
            _method_available(
                "agno.db.base",
                "AsyncBaseDb",
                ("get_learning", "upsert_learning", "delete_learning"),
            ),
        )
    )
    context_providers = _symbol_available("agno.context.provider", "ContextProvider")
    cancel_continue = _method_available(
        "agno.agent", "Agent", ("cancel_run", "continue_run", "acancel_run", "acontinue_run")
    )
    tool_batch_checkpoint = cancel_continue and _parameter_available(
        "agno.agent",
        "Agent",
        "__init__",
        "checkpoint",
    )
    agentos = _module_available("agno.os")
    environments = _module_available("agno.environments")
    filesystem = _symbol_available("agno.fs", "FileSystem")
    model_evaluation_subject = _method_available("agno.agent", "Agent", ("arun",)) and (
        _symbol_available("agno.run.agent", "RunOutput")
    )

    v3 = parsed.major >= 3
    v3_storage = v3 and _module_available("agno.db.migrations")
    v3_queue = v3 and _module_available("agno.job_queue")
    v3_events = v3 and _module_available("agno.os.event_streams")
    capabilities = (
        _status(
            AgnoFeature.LEARNING_MACHINE,
            learning_machine,
            "agno.learn.LearningMachine is importable" if learning_machine else "symbol missing",
        ),
        _status(
            AgnoFeature.LEARNED_KNOWLEDGE,
            learned_knowledge,
            "LearnedKnowledgeConfig is importable" if learned_knowledge else "symbol missing",
        ),
        _status(
            AgnoFeature.LEARNING_EXACT_NAME_INSPECTION,
            learning_exact_name_inspection,
            "VectorDb.name_exists is available for exact learning reconciliation"
            if learning_exact_name_inspection
            else "the exact vector-name inspection contract is missing",
        ),
        _status(
            AgnoFeature.SESSION_CONTEXT,
            session_context,
            "SessionContextConfig is importable" if session_context else "symbol missing",
        ),
        _status(
            AgnoFeature.LEARNING_ADMIN_CRUD,
            learning_admin_crud,
            "personal/session stores and sync/async database CRUD are available"
            if learning_admin_crud
            else "one or more personal/session CRUD contracts are missing",
        ),
        _status(
            AgnoFeature.MODEL_EVALUATION_SUBJECT,
            model_evaluation_subject,
            "Agent.arun and RunOutput are available for paired model subjects"
            if model_evaluation_subject
            else "the async Agent evaluation contract is missing",
        ),
        _status(
            AgnoFeature.CONTEXT_PROVIDERS,
            context_providers,
            "ContextProvider is importable" if context_providers else "symbol missing",
        ),
        _status(
            AgnoFeature.CANCEL_CONTINUE,
            cancel_continue,
            "sync/async cancel and continue methods exist"
            if cancel_continue
            else "one or more cancel/continue methods are missing",
        ),
        _status(
            AgnoFeature.TOOL_BATCH_CHECKPOINT,
            tool_batch_checkpoint,
            "Agent(checkpoint=...) and sync/async continuation are available"
            if tool_batch_checkpoint
            else "the tool-batch checkpoint constructor or continuation contract is missing",
        ),
        _status(
            AgnoFeature.AGENTOS,
            agentos,
            "agno.os package is installed" if agentos else "AgentOS package is missing",
        ),
        _status(
            AgnoFeature.EVALUATION_ENVIRONMENTS,
            environments,
            "agno.environments is available" if environments else "requires Agno 2.8+",
        ),
        _status(
            AgnoFeature.FILESYSTEM,
            filesystem,
            "agno.filesystem is available" if filesystem else "requires Agno 2.8+",
        ),
        _status(
            AgnoFeature.V3_NORMALIZED_RUN_STORAGE,
            v3_storage,
            "Agno 3 migration/runtime projection is present"
            if v3_storage
            else "Agno 3 component or optional dependency is unavailable",
        ),
        _status(
            AgnoFeature.V3_JOB_QUEUE,
            v3_queue,
            "Agno 3 job queue is present"
            if v3_queue
            else "Agno 3 component or optional dependency is unavailable",
        ),
        _status(
            AgnoFeature.V3_EVENT_STREAMS,
            v3_events,
            "Agno 3 event streams are present"
            if v3_events
            else "Agno 3 component or optional dependency is unavailable",
        ),
    )
    return AgnoCompatibilityReport(
        version=resolved_version,
        lane=lane,
        production_supported=lane
        in {AgnoLane.LEGACY, AgnoLane.STABLE, AgnoLane.STABLE_V3},
        preview=lane == AgnoLane.PREVIEW,
        capabilities=capabilities,
    )


def require_supported_agno(*, allow_preview: bool = False) -> AgnoCompatibilityReport:
    """Return the current compatibility report or raise for an unsupported Agno."""
    report = inspect_agno_compatibility()
    if report.production_supported or (allow_preview and report.preview):
        return report
    raise AgnoVersionError(
        version=report.version,
        reason=(
            f"supported production range is {STABLE_AGNO_SPEC}; Agno prereleases "
            "are certification previews only"
        ),
    )
