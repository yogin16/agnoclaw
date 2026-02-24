"""Runtime permission-mode controller inspired by Claude Code/OpenClaw flows."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Protocol, runtime_checkable

from .hooks import ToolCallRequest
from .policy import PolicyAction, PolicyDecision


class PermissionMode(str, Enum):
    """Supported runtime permission modes."""

    BYPASS = "bypass"
    DEFAULT = "default"
    ACCEPT_EDITS = "accept_edits"
    PLAN = "plan"
    DONT_ASK = "dont_ask"


_MODE_ALIASES = {
    "bypasspermissions": PermissionMode.BYPASS,
    "bypass_permissions": PermissionMode.BYPASS,
    "acceptedits": PermissionMode.ACCEPT_EDITS,
    "accept_edits": PermissionMode.ACCEPT_EDITS,
    "plan": PermissionMode.PLAN,
    "dontask": PermissionMode.DONT_ASK,
    "dont_ask": PermissionMode.DONT_ASK,
    "default": PermissionMode.DEFAULT,
    "bypass": PermissionMode.BYPASS,
}


READ_ONLY_TOOLS = frozenset(
    {
        "read_file",
        "glob_files",
        "grep_files",
        "list_dir",
        "web_search",
        "web_fetch",
        "list_todos",
        "read_progress",
        "read_features",
        "bash_output",
    }
)

FILE_EDIT_TOOLS = frozenset({"write_file", "edit_file", "multi_edit_file"})
EXEC_TOOLS = frozenset({"bash", "bash_start", "bash_kill"})
SUBAGENT_TOOLS = frozenset({"spawn_subagent"})


@dataclass(frozen=True)
class PermissionRequest:
    """Permission request emitted for approval workflows."""

    run_id: str
    tool_name: str
    category: str
    arguments: dict


@runtime_checkable
class PermissionApprover(Protocol):
    """Approver for default/accept-edits permission prompts."""

    def approve(self, request: PermissionRequest, context) -> bool | Awaitable[bool]:
        ...


def normalize_permission_mode(value: str | PermissionMode) -> PermissionMode:
    """Normalize aliases and validate permission mode values."""
    if isinstance(value, PermissionMode):
        return value
    raw = str(value or "").strip().lower()
    if raw in _MODE_ALIASES:
        return _MODE_ALIASES[raw]
    valid = ", ".join(sorted(m.value for m in PermissionMode))
    raise ValueError(f"Invalid permission mode: {value!r}. Use one of: {valid}")


def classify_tool(tool_name: str) -> tuple[str, bool]:
    """Return (category, is_read_only) for a tool name."""
    name = str(tool_name or "").strip()
    if name in FILE_EDIT_TOOLS:
        return ("file_edit", False)
    if name in EXEC_TOOLS:
        return ("exec", False)
    if name in SUBAGENT_TOOLS:
        return ("subagent", False)
    if name in READ_ONLY_TOOLS:
        return ("read", True)
    if name.startswith("create_") or name.startswith("update_") or name.startswith("delete_"):
        return ("state_mutation", False)
    if name.startswith("write_") or name.startswith("edit_"):
        return ("state_mutation", False)
    return ("other", False)


class PermissionController:
    """Runtime permission-mode evaluator for tool calls."""

    def __init__(
        self,
        *,
        mode: str | PermissionMode = PermissionMode.BYPASS,
        approver: PermissionApprover | None = None,
        require_approver: bool = False,
        preapproved_tools: tuple[str, ...] = (),
        preapproved_categories: tuple[str, ...] = (),
    ) -> None:
        self.mode = normalize_permission_mode(mode)
        self.approver = approver
        self.require_approver = require_approver
        self._approved_tools = set(preapproved_tools)
        self._approved_categories = set(preapproved_categories)

    def set_mode(self, mode: str | PermissionMode) -> None:
        self.mode = normalize_permission_mode(mode)

    def current_mode(self) -> PermissionMode:
        return self.mode

    def check_tool_call(
        self,
        request: ToolCallRequest,
        context,
        *,
        resolve_sync_value,
    ) -> PolicyDecision:
        """Evaluate tool call permissions based on the active mode."""
        category, is_read_only = classify_tool(request.tool_name)
        mode = self.mode

        if mode == PermissionMode.BYPASS:
            return PolicyDecision(
                action=PolicyAction.ALLOW,
                reason_code="PERMISSION_BYPASS",
                message="Permission checks bypassed for this run.",
            )

        if mode == PermissionMode.PLAN:
            if is_read_only:
                return PolicyDecision(
                    action=PolicyAction.ALLOW,
                    reason_code="PERMISSION_PLAN_READ_ALLOWED",
                )
            return PolicyDecision.deny(
                reason_code="PERMISSION_PLAN_READ_ONLY",
                message=f"Plan mode is read-only. Tool '{request.tool_name}' is not allowed.",
            )

        if self._is_preapproved(request, category, context):
            return PolicyDecision(
                action=PolicyAction.ALLOW,
                reason_code="PERMISSION_PREAPPROVED",
            )

        if mode == PermissionMode.DONT_ASK:
            return PolicyDecision.deny(
                reason_code="PERMISSION_DONT_ASK_DENIED",
                message=(
                    f"Tool '{request.tool_name}' requires approval and mode is 'dont_ask'. "
                    "Pre-approve this tool/category to allow it."
                ),
            )

        if mode == PermissionMode.ACCEPT_EDITS and category == "file_edit":
            self._approved_categories.add("file_edit")
            return PolicyDecision(
                action=PolicyAction.ALLOW,
                reason_code="PERMISSION_ACCEPT_EDITS_AUTO_ALLOW",
            )

        if mode == PermissionMode.DEFAULT and category == "read":
            return PolicyDecision(
                action=PolicyAction.ALLOW,
                reason_code="PERMISSION_DEFAULT_READ_ALLOWED",
            )

        if self.approver is None:
            if self.require_approver:
                return PolicyDecision.deny(
                    reason_code="PERMISSION_APPROVER_REQUIRED",
                    message=(
                        f"Tool '{request.tool_name}' requires approval, but no approver is configured."
                    ),
                )
            return PolicyDecision(
                action=PolicyAction.ALLOW,
                reason_code="PERMISSION_NO_APPROVER_ALLOW",
                message="No approver configured; allowing tool call.",
            )

        allowed = resolve_sync_value(
            self.approver.approve(
                PermissionRequest(
                    run_id=request.run_id,
                    tool_name=request.tool_name,
                    category=category,
                    arguments=dict(request.arguments),
                ),
                context,
            ),
            operation=f"permission.approve:{request.tool_name}",
        )
        if bool(allowed):
            self._approved_tools.add(request.tool_name)
            self._approved_categories.add(category)
            return PolicyDecision(
                action=PolicyAction.ALLOW,
                reason_code="PERMISSION_APPROVED",
            )
        return PolicyDecision.deny(
            reason_code="PERMISSION_REJECTED",
            message=f"Permission rejected for tool '{request.tool_name}'.",
        )

    def _is_preapproved(self, request: ToolCallRequest, category: str, context) -> bool:
        if request.tool_name in self._approved_tools or category in self._approved_categories:
            return True

        metadata = getattr(context, "metadata", None) or {}
        approved_tools = metadata.get("permission_preapproved_tools") or ()
        approved_categories = metadata.get("permission_preapproved_categories") or ()
        if request.tool_name in set(str(v) for v in approved_tools):
            return True
        if category in set(str(v) for v in approved_categories):
            return True
        return False

