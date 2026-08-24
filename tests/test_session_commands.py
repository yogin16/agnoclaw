"""Contracts for session command routing and executor ownership."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agnoclaw.runtime import ElevatedSessionMode
from agnoclaw.session_commands import _ElevatedSessionCommandExecutor
from agnoclaw.tools.backends import (
    BackgroundCommandHandle,
    BackgroundCommandOutput,
    CommandResult,
)


def _router(
    mode: ElevatedSessionMode,
    *,
    owns_sandbox_executor: bool = False,
) -> tuple[_ElevatedSessionCommandExecutor, MagicMock, MagicMock, MagicMock]:
    harness = MagicMock()
    harness._elevated_session_mode = mode
    sandbox = MagicMock()
    host = MagicMock()
    router = _ElevatedSessionCommandExecutor(
        harness=harness,
        sandbox_executor=sandbox,
        host_executor=host,
        owns_sandbox_executor=owns_sandbox_executor,
    )
    return router, harness, sandbox, host


def test_session_command_router_uses_sandbox_when_elevation_is_off() -> None:
    router, harness, sandbox, host = _router(ElevatedSessionMode.OFF)
    run_result = CommandResult(stdout="safe", stderr="", exit_code=0, duration_ms=1)
    handle = BackgroundCommandHandle(
        task_id="sandbox-task",
        pid=17,
        status="running",
        log_path="/tmp/sandbox.log",
    )
    output = BackgroundCommandOutput(
        task_id="sandbox-task",
        status="running",
        output="safe",
        pid=17,
    )
    sandbox.run.return_value = run_result
    sandbox.start.return_value = handle
    sandbox.output.return_value = output
    sandbox.kill.return_value = "killed"

    assert router.run(command="echo safe", workdir="/workspace", timeout_seconds=3) is run_result
    assert router.start(command="sleep 1", workdir=None, description="safe") is handle
    assert router.output(task_id="sandbox-task") is output
    assert router.kill(task_id="sandbox-task") == "killed"

    harness.run_elevated_command.assert_not_called()
    host.run.assert_not_called()
    host.start.assert_not_called()


@pytest.mark.parametrize(
    ("mode", "skip_approval"),
    [
        (ElevatedSessionMode.ON, False),
        (ElevatedSessionMode.FULL, True),
    ],
)
def test_session_command_router_uses_governed_foreground_path(
    mode: ElevatedSessionMode,
    skip_approval: bool,
) -> None:
    router, harness, sandbox, _host = _router(mode)
    harness.run_elevated_command.return_value = SimpleNamespace(
        stdout="host",
        stderr="warning",
        exit_code=2,
        duration_ms=9,
    )

    result = router.run(command="host-check", workdir="/host", timeout_seconds=5)

    assert result == CommandResult(
        stdout="host",
        stderr="warning",
        exit_code=2,
        duration_ms=9,
    )
    harness.run_elevated_command.assert_called_once_with(
        "host-check",
        reason="Session elevated bash tool call.",
        working_dir="/host",
        timeout_seconds=5,
        metadata={"source": "elevated_session", "elevated_mode": mode.value},
        _skip_approval=skip_approval,
    )
    sandbox.run.assert_not_called()


def test_session_command_router_tracks_elevated_background_tasks() -> None:
    router, harness, sandbox, host = _router(ElevatedSessionMode.ASK)
    request = SimpleNamespace(run_id="run-1", command="host-job", working_dir="/host")
    context = object()
    handle = BackgroundCommandHandle(
        task_id="host-task",
        pid=29,
        status="running",
        log_path="/tmp/host.log",
    )
    output = BackgroundCommandOutput(
        task_id="host-task",
        status="running",
        output="working",
        pid=29,
    )
    harness._build_elevated_command_request.return_value = (request, context)
    harness._elevated_tool_request.return_value = "tool-request"
    harness._elevated_request_payload.return_value = {"request": "digest"}
    host.start.return_value = handle
    host.output.return_value = output
    host.kill.return_value = "host-killed"

    assert router.start(command="host-job", workdir="/host", description="audit") is handle
    assert router.output(task_id="host-task") is output
    assert router.kill(task_id="host-task", force=True) == "host-killed"

    harness._authorize_elevated_command_sync.assert_called_once_with(
        request=request,
        tool_request="tool-request",
        context=context,
        payload={"request": "digest"},
        skip_approval=False,
    )
    assert [call.kwargs["event_type"] for call in harness._emit_event_sync.call_args_list] == [
        "elevated.command.started",
        "elevated.command.completed",
    ]
    sandbox.start.assert_not_called()
    sandbox.output.assert_not_called()
    sandbox.kill.assert_not_called()


def test_session_command_router_closes_only_owned_executors() -> None:
    borrowed, _harness, borrowed_sandbox, borrowed_host = _router(ElevatedSessionMode.OFF)
    owned, _harness, owned_sandbox, owned_host = _router(
        ElevatedSessionMode.OFF,
        owns_sandbox_executor=True,
    )

    borrowed.close()
    borrowed.close()
    owned.close()
    owned.close()

    borrowed_host.close.assert_called_once_with()
    borrowed_sandbox.close.assert_not_called()
    owned_host.close.assert_called_once_with()
    owned_sandbox.close.assert_called_once_with()
