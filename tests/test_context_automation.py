"""Session-local admission tests for automatic context maintenance."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from agnoclaw.context_automation import ContextAutomationCoordinator
from agnoclaw.runtime.errors import HarnessError


def test_active_run_blocks_same_session_maintenance_until_release():
    coordinator = ContextAutomationCoordinator()
    run = coordinator.admit_run("session-1")

    with pytest.raises(HarnessError) as caught:
        coordinator.begin_maintenance("session-1")

    assert caught.value.code == "CONTEXT_SESSION_BUSY"
    assert caught.value.retryable is True
    assert run is not None
    run.release()
    coordinator.begin_maintenance("session-1").release()


def test_maintenance_blocks_external_run_but_allows_owned_internal_run():
    coordinator = ContextAutomationCoordinator()
    maintenance = coordinator.begin_maintenance("session-1")

    with pytest.raises(HarnessError) as caught:
        coordinator.admit_run("session-1")

    assert caught.value.code == "CONTEXT_MAINTENANCE_IN_PROGRESS"
    internal = coordinator.admit_run("session-1", maintenance_owned=True)
    assert internal is not None
    internal.release()
    maintenance.release()


def test_context_leases_are_idempotent_and_sessions_are_independent():
    coordinator = ContextAutomationCoordinator()
    run = coordinator.admit_run("session-1")
    assert run is not None

    other = coordinator.begin_maintenance("session-2")
    other.release()
    run.release()
    run.release()

    coordinator.begin_maintenance("session-1").release()
    assert coordinator.admit_run(None) is None


def test_context_lease_concurrent_release_decrements_once():
    coordinator = ContextAutomationCoordinator()
    lease = coordinator.admit_run("session-1")
    assert lease is not None

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: lease.release(), range(32)))

    coordinator.begin_maintenance("session-1").release()


def test_owned_maintenance_requires_exactly_one_active_run_and_fences_new_work():
    coordinator = ContextAutomationCoordinator()
    with pytest.raises(HarnessError) as absent:
        coordinator.begin_owned_maintenance("session-1")
    assert absent.value.code == "CONTEXT_SESSION_BUSY"

    active = coordinator.admit_run("session-1")
    assert active is not None
    maintenance = coordinator.begin_owned_maintenance("session-1")
    with pytest.raises(HarnessError) as fenced:
        coordinator.admit_run("session-1")
    assert fenced.value.code == "CONTEXT_MAINTENANCE_IN_PROGRESS"

    maintenance.release()
    active.release()


def test_owned_maintenance_rejects_multiple_active_runs():
    coordinator = ContextAutomationCoordinator()
    first = coordinator.admit_run("session-1")
    second = coordinator.admit_run("session-1")
    assert first is not None and second is not None

    with pytest.raises(HarnessError) as caught:
        coordinator.begin_owned_maintenance("session-1")

    assert caught.value.code == "CONTEXT_SESSION_BUSY"
    first.release()
    second.release()
