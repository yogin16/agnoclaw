"""Cooperative cross-process context reader/writer lock contracts."""

from __future__ import annotations

import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agnoclaw import (
    ContextLockLostError,
    ContextLockMode,
    ContextLockUnavailableError,
    ContextScope,
    LocalFileContextLockProvider,
)


def _scope(session_id: str = "session-1") -> ContextScope:
    return ContextScope(
        tenant_id="tenant-1",
        user_id="user-1",
        session_id=session_id,
    )


def test_shared_readers_coexist_and_exclusive_writer_fails_fast(tmp_path: Path) -> None:
    provider = LocalFileContextLockProvider(tmp_path / "locks")
    first = provider.acquire(_scope(), mode=ContextLockMode.SHARED)
    second = provider.acquire(_scope(), mode=ContextLockMode.SHARED)

    with pytest.raises(ContextLockUnavailableError) as blocked:
        provider.acquire(_scope(), mode=ContextLockMode.EXCLUSIVE)

    assert blocked.value.retryable is True
    assert blocked.value.details == {
        "scope_digest": _scope().digest,
        "mode": "exclusive",
    }
    first.validate()
    second.validate()
    lock_files = list((tmp_path / "locks").iterdir())
    assert [path.name for path in lock_files] == [f"{_scope().digest}.lock"]
    assert "session-1" not in lock_files[0].name
    assert stat.S_IMODE(lock_files[0].stat().st_mode) == 0o600
    second.release()
    first.release()


def test_exclusive_writer_blocks_readers_but_not_another_scope(tmp_path: Path) -> None:
    provider = LocalFileContextLockProvider(tmp_path / "locks")
    writer = provider.acquire(_scope(), mode=ContextLockMode.EXCLUSIVE)

    with pytest.raises(ContextLockUnavailableError):
        provider.acquire(_scope(), mode=ContextLockMode.SHARED)
    other = provider.acquire(_scope("session-2"), mode=ContextLockMode.SHARED)

    other.release()
    writer.release()
    writer.release()
    with pytest.raises(ContextLockLostError):
        writer.validate()


def test_reader_upgrade_fails_safe_with_competitor_then_succeeds_when_sole(
    tmp_path: Path,
) -> None:
    provider = LocalFileContextLockProvider(tmp_path / "locks")
    first = provider.acquire(_scope(), mode=ContextLockMode.SHARED)
    second = provider.acquire(_scope(), mode=ContextLockMode.SHARED)

    with pytest.raises(ContextLockUnavailableError):
        first.upgrade()
    first.validate()
    assert first.mode is ContextLockMode.SHARED

    second.release()
    first.upgrade()
    first.validate()
    assert first.mode is ContextLockMode.EXCLUSIVE
    with pytest.raises(ContextLockUnavailableError):
        provider.acquire(_scope(), mode=ContextLockMode.SHARED)
    first.release()


def test_real_child_process_reader_fences_parent_writer(tmp_path: Path) -> None:
    root = tmp_path / "locks"
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    code = """
import sys, time
from pathlib import Path
from agnoclaw import ContextLockMode, ContextScope, LocalFileContextLockProvider
root, ready, release = map(Path, sys.argv[1:])
scope = ContextScope(tenant_id='tenant-1', user_id='user-1', session_id='session-1')
lease = LocalFileContextLockProvider(root).acquire(scope, mode=ContextLockMode.SHARED)
ready.write_text('ready', encoding='utf-8')
deadline = time.monotonic() + 15
while not release.exists() and time.monotonic() < deadline:
    time.sleep(0.01)
lease.release()
raise SystemExit(0 if release.exists() else 3)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(root), str(ready), str(release)],
        cwd=Path(__file__).resolve().parents[1],
    )
    try:
        deadline = 500
        while not ready.exists() and deadline:
            if child.poll() is not None:
                raise AssertionError(f"lock child exited early with {child.returncode}")
            deadline -= 1
            time.sleep(0.01)
        assert ready.exists()
        provider = LocalFileContextLockProvider(root)
        with pytest.raises(ContextLockUnavailableError):
            provider.acquire(_scope(), mode=ContextLockMode.EXCLUSIVE)
        release.write_text("release", encoding="utf-8")
        assert child.wait(timeout=10) == 0
        writer = provider.acquire(_scope(), mode=ContextLockMode.EXCLUSIVE)
        writer.release()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
