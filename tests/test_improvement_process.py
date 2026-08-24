"""Fresh-process improvement subject protocol, containment, and cleanup contracts."""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

import pytest

from agnoclaw import (
    EvaluationCase,
    EvaluationSlice,
    ProcessEvaluationExitedError,
    ProcessEvaluationOutputLimitError,
    ProcessEvaluationProtocolError,
    ProcessEvaluationRemoteError,
    ProcessEvaluationSubject,
    ProcessEvaluationSubjectFactory,
)

WORKER = Path(__file__).parent / "fixtures" / "improvement_process_worker.py"


def _case() -> EvaluationCase:
    return EvaluationCase(
        case_id="process-case",
        slice=EvaluationSlice.HELD_IN,
        task_class="process-contract",
        payload={"baseline_quality": 0.5},
    )


async def _execute(subject: ProcessEvaluationSubject):
    await subject.asetup()
    try:
        return await subject(_case())
    finally:
        await subject.aclose()


async def _wait_for_file(path: Path) -> None:
    for _ in range(200):
        if path.exists():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("child process did not publish its test marker")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _assert_pid_stopped(pid: int) -> None:
    for _ in range(200):
        if not _pid_alive(pid):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"child process {pid} remained alive")


async def test_process_subject_uses_fresh_state_minimal_environment_and_redacted_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGNOCLAW_PROCESS_PARENT_SECRET", "must-not-inherit")
    secret = "explicit-but-redacted-secret"
    factory = ProcessEvaluationSubjectFactory(
        (sys.executable, str(WORKER), "success"),
        environment={"AGNOCLAW_PROCESS_EXPLICIT": secret},
    )

    first = await _execute(factory())
    second = await _execute(factory())

    assert first.output["parent_secret_present"] is False
    assert first.output["explicit_environment"] == secret
    assert first.output["pid"] != second.output["pid"]
    assert first.output["cwd"] != second.output["cwd"]
    assert not Path(first.output["cwd"]).exists()
    assert not Path(second.output["cwd"]).exists()
    assert first.tokens == 2
    assert first.cost_usd == pytest.approx(0.02)
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", factory.subject_contract_digest)
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", factory.subject_isolation_digest)
    assert secret not in repr(factory)
    assert "AGNOCLAW_PROCESS_EXPLICIT" not in repr(factory)


async def test_process_subject_rejects_remote_errors_invalid_protocol_and_stream_overflow() -> (
    None
):
    remote = ProcessEvaluationSubject((sys.executable, str(WORKER), "error"))
    await remote.asetup()
    with pytest.raises(ProcessEvaluationRemoteError) as remote_error:
        await remote(_case())
    await remote.aclose()
    assert remote_error.value.remote_error_type == "ValueError"
    assert "sensitive" not in str(remote_error.value)

    invalid = ProcessEvaluationSubject((sys.executable, str(WORKER), "invalid"))
    await invalid.asetup()
    with pytest.raises(ProcessEvaluationProtocolError) as protocol_error:
        await invalid(_case())
    await invalid.aclose()
    assert "private response" not in str(protocol_error.value)

    overflow = ProcessEvaluationSubject(
        (sys.executable, str(WORKER), "oversize"),
        max_stdout_bytes=1024,
    )
    await overflow.asetup()
    with pytest.raises(ProcessEvaluationOutputLimitError) as output_error:
        await overflow(_case())
    await overflow.aclose()
    assert output_error.value.stream == "stdout"


async def test_process_subject_discards_child_stderr_and_reports_only_exit_contract() -> None:
    subject = ProcessEvaluationSubject((sys.executable, str(WORKER), "crash"))
    await subject.asetup()
    with pytest.raises(ProcessEvaluationExitedError) as error:
        await subject(_case())
    await subject.aclose()

    assert error.value.returncode == 7
    assert "api-key" not in str(error.value)
    assert "must-not-enter" not in repr(error.value)


async def test_process_subject_cancellation_reaps_the_child(tmp_path: Path) -> None:
    marker = tmp_path / "worker.pid"
    subject = ProcessEvaluationSubject(
        (sys.executable, str(WORKER), "hang", str(marker)),
        terminate_grace_seconds=0.05,
    )
    await subject.asetup()
    task = asyncio.create_task(subject(_case()))
    await _wait_for_file(marker)
    pid = int(marker.read_text(encoding="utf-8"))

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await subject.aclose()

    await _assert_pid_stopped(pid)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group contract")
async def test_process_subject_cancellation_reaps_its_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "process-group.pids"
    subject = ProcessEvaluationSubject(
        (sys.executable, str(WORKER), "spawn", str(marker)),
        terminate_grace_seconds=0.05,
    )
    await subject.asetup()
    task = asyncio.create_task(subject(_case()))
    await _wait_for_file(marker)
    parent_pid, child_pid = [
        int(value) for value in marker.read_text(encoding="utf-8").splitlines()
    ]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await subject.aclose()

    await _assert_pid_stopped(parent_pid)
    await _assert_pid_stopped(child_pid)


def test_process_subject_configuration_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        ProcessEvaluationSubject(("python", str(WORKER), "success"))
    with pytest.raises(ValueError, match="existing directory"):
        ProcessEvaluationSubject(
            (sys.executable, str(WORKER), "success"),
            working_directory=tmp_path / "missing",
        )
    with pytest.raises(ValueError, match="portable identifier"):
        ProcessEvaluationSubject(
            (sys.executable, str(WORKER), "success"),
            environment={"BAD-NAME": "value"},
        )
    with pytest.raises(ValueError, match="max_stdout_bytes"):
        ProcessEvaluationSubject(
            (sys.executable, str(WORKER), "success"),
            max_stdout_bytes=0,
        )
