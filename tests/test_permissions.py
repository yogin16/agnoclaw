"""Tests for the runtime permission controller."""

from unittest.mock import MagicMock

import pytest

from agnoclaw.runtime.hooks import ToolCallRequest
from agnoclaw.runtime.permissions import (
    PermissionController,
    PermissionMode,
    PermissionRequest,
    classify_tool,
    normalize_permission_mode,
)
from agnoclaw.runtime.policy import PolicyAction


def _make_request(tool_name: str = "bash", run_id: str = "r1", **kwargs) -> ToolCallRequest:
    return ToolCallRequest(
        run_id=run_id,
        tool_name=tool_name,
        arguments=kwargs,
    )


def _identity(x, **kwargs):
    """Pass-through resolve_sync_value."""
    return x


# ── normalize_permission_mode ────────────────────────────────────────────


def test_normalize_bypass():
    assert normalize_permission_mode("bypass") == PermissionMode.BYPASS


def test_normalize_accept_edits_alias():
    assert normalize_permission_mode("acceptedits") == PermissionMode.ACCEPT_EDITS
    assert normalize_permission_mode("accept_edits") == PermissionMode.ACCEPT_EDITS


def test_normalize_dont_ask_alias():
    assert normalize_permission_mode("dontask") == PermissionMode.DONT_ASK


def test_normalize_invalid_raises():
    with pytest.raises(ValueError, match="Invalid permission mode"):
        normalize_permission_mode("nonexistent")


def test_normalize_passthrough_enum():
    assert normalize_permission_mode(PermissionMode.PLAN) == PermissionMode.PLAN


# ── classify_tool ────────────────────────────────────────────────────────


def test_classify_read_only():
    cat, read_only = classify_tool("read_file")
    assert cat == "read"
    assert read_only is True


def test_classify_file_edit():
    cat, read_only = classify_tool("write_file")
    assert cat == "file_edit"
    assert read_only is False


def test_classify_exec():
    cat, read_only = classify_tool("bash")
    assert cat == "exec"
    assert read_only is False


def test_classify_subagent():
    cat, read_only = classify_tool("spawn_subagent")
    assert cat == "subagent"
    assert read_only is False


def test_classify_create_prefix():
    cat, _ = classify_tool("create_todo")
    assert cat == "state_mutation"


def test_classify_update_prefix():
    cat, _ = classify_tool("update_feature_status")
    assert cat == "state_mutation"


def test_classify_write_prefix():
    cat, _ = classify_tool("write_progress")
    assert cat == "state_mutation"


def test_classify_unknown():
    cat, read_only = classify_tool("unknown_tool")
    assert cat == "other"
    assert read_only is False


# ── PermissionController — bypass mode ──────────────────────────────────


def test_bypass_allows_everything():
    ctrl = PermissionController(mode=PermissionMode.BYPASS)
    req = _make_request("bash")
    decision = ctrl.check_tool_call(req, None, resolve_sync_value=_identity)
    assert decision.action == PolicyAction.ALLOW
    assert decision.reason_code == "PERMISSION_BYPASS"


# ── plan mode ───────────────────────────────────────────────────────────


def test_plan_allows_read_only():
    ctrl = PermissionController(mode=PermissionMode.PLAN)
    req = _make_request("read_file")
    decision = ctrl.check_tool_call(req, None, resolve_sync_value=_identity)
    assert decision.action == PolicyAction.ALLOW


def test_plan_denies_write():
    ctrl = PermissionController(mode=PermissionMode.PLAN)
    req = _make_request("bash")
    decision = ctrl.check_tool_call(req, None, resolve_sync_value=_identity)
    assert decision.action == PolicyAction.DENY
    assert "read-only" in decision.message.lower()


# ── dont_ask mode ───────────────────────────────────────────────────────


def test_dont_ask_denies_non_preapproved():
    ctrl = PermissionController(mode=PermissionMode.DONT_ASK)
    req = _make_request("bash")
    decision = ctrl.check_tool_call(req, None, resolve_sync_value=_identity)
    assert decision.action == PolicyAction.DENY
    assert "dont_ask" in decision.message


def test_dont_ask_allows_preapproved_tool():
    ctrl = PermissionController(
        mode=PermissionMode.DONT_ASK,
        preapproved_tools=("bash",),
    )
    req = _make_request("bash")
    decision = ctrl.check_tool_call(req, None, resolve_sync_value=_identity)
    assert decision.action == PolicyAction.ALLOW
    assert decision.reason_code == "PERMISSION_PREAPPROVED"


# ── accept_edits mode ──────────────────────────────────────────────────


def test_accept_edits_allows_file_edit():
    ctrl = PermissionController(mode=PermissionMode.ACCEPT_EDITS)
    req = _make_request("write_file")
    decision = ctrl.check_tool_call(req, None, resolve_sync_value=_identity)
    assert decision.action == PolicyAction.ALLOW
    assert decision.reason_code == "PERMISSION_ACCEPT_EDITS_AUTO_ALLOW"


# ── default mode ────────────────────────────────────────────────────────


def test_default_allows_read():
    ctrl = PermissionController(mode=PermissionMode.DEFAULT)
    req = _make_request("read_file")
    decision = ctrl.check_tool_call(req, None, resolve_sync_value=_identity)
    assert decision.action == PolicyAction.ALLOW
    assert decision.reason_code == "PERMISSION_DEFAULT_READ_ALLOWED"


def test_default_no_approver_allows():
    """Without approver, non-read tools are allowed with a warning."""
    ctrl = PermissionController(mode=PermissionMode.DEFAULT)
    req = _make_request("bash")
    decision = ctrl.check_tool_call(req, None, resolve_sync_value=_identity)
    assert decision.action == PolicyAction.ALLOW
    assert decision.reason_code == "PERMISSION_NO_APPROVER_ALLOW"


def test_default_require_approver_denies():
    """require_approver=True denies when no approver is set."""
    ctrl = PermissionController(mode=PermissionMode.DEFAULT, require_approver=True)
    req = _make_request("bash")
    decision = ctrl.check_tool_call(req, None, resolve_sync_value=_identity)
    assert decision.action == PolicyAction.DENY
    assert decision.reason_code == "PERMISSION_APPROVER_REQUIRED"


# ── approver flow ───────────────────────────────────────────────────────


def test_approver_approves():
    approver = MagicMock()
    approver.approve.return_value = True

    ctrl = PermissionController(mode=PermissionMode.DEFAULT, approver=approver)
    req = _make_request("bash")
    decision = ctrl.check_tool_call(req, None, resolve_sync_value=_identity)
    assert decision.action == PolicyAction.ALLOW
    assert decision.reason_code == "PERMISSION_APPROVED"


def test_approver_rejects():
    approver = MagicMock()
    approver.approve.return_value = False

    ctrl = PermissionController(mode=PermissionMode.DEFAULT, approver=approver)
    req = _make_request("bash")
    decision = ctrl.check_tool_call(req, None, resolve_sync_value=_identity)
    assert decision.action == PolicyAction.DENY
    assert decision.reason_code == "PERMISSION_REJECTED"


# ── preapproval via context metadata ────────────────────────────────────


def test_preapproved_via_context_metadata_tool():
    ctrl = PermissionController(mode=PermissionMode.DEFAULT)
    req = _make_request("bash")
    ctx = MagicMock()
    ctx.metadata = {"permission_preapproved_tools": ["bash"]}
    decision = ctrl.check_tool_call(req, ctx, resolve_sync_value=_identity)
    assert decision.action == PolicyAction.ALLOW
    assert decision.reason_code == "PERMISSION_PREAPPROVED"


def test_preapproved_via_context_metadata_category():
    ctrl = PermissionController(mode=PermissionMode.DEFAULT)
    req = _make_request("bash")
    ctx = MagicMock()
    ctx.metadata = {"permission_preapproved_categories": ["exec"]}
    decision = ctrl.check_tool_call(req, ctx, resolve_sync_value=_identity)
    assert decision.action == PolicyAction.ALLOW
    assert decision.reason_code == "PERMISSION_PREAPPROVED"


# ── set_mode / current_mode ─────────────────────────────────────────────


def test_set_mode_and_current():
    ctrl = PermissionController(mode="bypass")
    assert ctrl.current_mode() == PermissionMode.BYPASS
    ctrl.set_mode("plan")
    assert ctrl.current_mode() == PermissionMode.PLAN


# ── PermissionRequest dataclass ─────────────────────────────────────────


def test_permission_request_fields():
    pr = PermissionRequest(run_id="r1", tool_name="bash", category="exec", arguments={"cmd": "ls"})
    assert pr.run_id == "r1"
    assert pr.tool_name == "bash"
    assert pr.arguments == {"cmd": "ls"}
