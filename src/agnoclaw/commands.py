"""Versioned public control commands for live agnoclaw runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar
from uuid import uuid4


def _command_id() -> str:
    return f"cmd_{uuid4().hex}"


def _require_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _freeze_data(value: Any) -> Any:
    # Local imports keep this public command module independent from the runtime
    # package's export order while reusing the canonical admission freezer.
    from .runtime.security import freeze_data

    return freeze_data(value)


def _thaw_data(value: Any) -> Any:
    from .runtime.security import thaw_data

    return thaw_data(value)


@dataclass(frozen=True)
class Pause:
    """Request an owner-local pause at the next certified safe point."""

    command_type: ClassVar[str] = "pause"
    schema_version: ClassVar[str] = "1.0"
    reason: str | None = None
    command_id: str = field(default_factory=_command_id)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "command_id", _require_text(self.command_id, field_name="command_id")
        )
        if self.reason is not None:
            object.__setattr__(self, "reason", _require_text(self.reason, field_name="reason"))


@dataclass(frozen=True)
class Resume:
    """Resume an owner-local worker from its certified continuation boundary."""

    command_type: ClassVar[str] = "resume"
    schema_version: ClassVar[str] = "1.0"
    command_id: str = field(default_factory=_command_id)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "command_id", _require_text(self.command_id, field_name="command_id")
        )


@dataclass(frozen=True)
class Respond:
    """Supply input or an approval response to one exact pending request."""

    request_id: str
    payload: Any
    command_id: str = field(default_factory=_command_id)
    command_type: ClassVar[str] = "respond"
    schema_version: ClassVar[str] = "1.0"

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "request_id", _require_text(self.request_id, field_name="request_id")
        )
        object.__setattr__(
            self, "command_id", _require_text(self.command_id, field_name="command_id")
        )
        object.__setattr__(self, "payload", _freeze_data(self.payload))


@dataclass(frozen=True)
class Steer:
    """Add owner-local guidance before a run closes its steering safe point."""

    instruction: str
    command_id: str = field(default_factory=_command_id)
    command_type: ClassVar[str] = "steer"
    schema_version: ClassVar[str] = "1.0"

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "instruction", _require_text(self.instruction, field_name="instruction")
        )
        object.__setattr__(
            self, "command_id", _require_text(self.command_id, field_name="command_id")
        )


@dataclass(frozen=True)
class Fork:
    """Request a new run from one certified source checkpoint."""

    from_step: int | str
    command_id: str = field(default_factory=_command_id)
    command_type: ClassVar[str] = "fork"
    schema_version: ClassVar[str] = "1.0"

    def __post_init__(self) -> None:
        if isinstance(self.from_step, bool) or not isinstance(self.from_step, (int, str)):
            raise ValueError("from_step must be a non-negative integer or non-empty string")
        if isinstance(self.from_step, int) and self.from_step < 0:
            raise ValueError("from_step must be non-negative")
        if isinstance(self.from_step, str):
            object.__setattr__(
                self,
                "from_step",
                _require_text(self.from_step, field_name="from_step"),
            )
        object.__setattr__(
            self, "command_id", _require_text(self.command_id, field_name="command_id")
        )


RunCommand = Pause | Resume | Respond | Steer | Fork


def command_to_dict(command: RunCommand) -> dict[str, Any]:
    """Return the canonical JSON-compatible representation of a command."""
    payload: dict[str, Any] = {
        "schema_version": command.schema_version,
        "command_type": command.command_type,
        "command_id": command.command_id,
    }
    if isinstance(command, Pause):
        payload["reason"] = command.reason
    elif isinstance(command, Respond):
        payload.update(
            request_id=command.request_id,
            payload=_thaw_data(command.payload),
        )
    elif isinstance(command, Steer):
        payload["instruction"] = command.instruction
    elif isinstance(command, Fork):
        payload["from_step"] = command.from_step
    return payload


def command_from_dict(value: dict[str, Any]) -> RunCommand:
    """Parse one command while rejecting unknown versions, types, and fields."""
    if not isinstance(value, dict):
        raise ValueError("run command must be an object")
    version = value.get("schema_version")
    if version != "1.0":
        raise ValueError(f"unsupported run command schema_version: {version!r}")
    command_type = value.get("command_type")
    common = {"schema_version", "command_type", "command_id"}
    constructors: dict[str, tuple[type, set[str]]] = {
        "pause": (Pause, common | {"reason"}),
        "resume": (Resume, common),
        "respond": (Respond, common | {"request_id", "payload"}),
        "steer": (Steer, common | {"instruction"}),
        "fork": (Fork, common | {"from_step"}),
    }
    selected = constructors.get(str(command_type))
    if selected is None:
        raise ValueError(f"unsupported run command type: {command_type!r}")
    constructor, allowed = selected
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"unknown run command field(s): {', '.join(sorted(unknown))}")
    kwargs = {key: item for key, item in value.items() if key not in common}
    kwargs["command_id"] = value.get("command_id")
    return constructor(**kwargs)


__all__ = [
    "Fork",
    "Pause",
    "Respond",
    "Resume",
    "RunCommand",
    "Steer",
    "command_from_dict",
    "command_to_dict",
]
