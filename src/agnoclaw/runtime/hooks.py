"""Hook contracts and run envelope dataclasses."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Protocol, runtime_checkable


@dataclass
class RunInput:
    """Normalized run input passed through policy/hooks."""

    run_id: str
    message: str
    skill: str | None
    stream: bool
    stream_events: bool
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PromptEnvelope:
    """Prompt payload visible to pre-send policy checks."""

    system_prompt: str
    user_message: str
    skill: str | None = None


@dataclass
class SkillLoadRequest:
    """Skill activation request payload."""

    name: str
    arguments: str = ""


@dataclass
class ToolCallRequest:
    """Tool invocation request payload."""

    run_id: str
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCallResult:
    """Tool invocation result payload."""

    run_id: str
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    output: Any = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunResultEnvelope:
    """Run result payload passed to post-hooks."""

    run_id: str
    content: Any
    raw_output: Any
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class PreRunHook(Protocol):
    """Pre-run hook protocol."""

    def __call__(self, run_input: RunInput, context) -> RunInput | None | Awaitable[RunInput | None]:
        ...


@runtime_checkable
class PostRunHook(Protocol):
    """Post-run hook protocol."""

    def __call__(
        self,
        run_input: RunInput,
        result: RunResultEnvelope,
        context,
    ) -> RunResultEnvelope | None | Awaitable[RunResultEnvelope | None]:
        ...
