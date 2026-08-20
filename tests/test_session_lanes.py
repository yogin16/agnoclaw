"""Concurrency contracts for bounded exact-session execution lanes."""

from __future__ import annotations

import asyncio

import pytest

from agnoclaw.runtime.concurrency import (
    AsyncSessionLanes,
    RuntimeAdmissionOverloadedError,
)


async def _wait_for_stat(lanes: AsyncSessionLanes, key: str, value: int) -> None:
    for _ in range(100):
        if lanes.admission_stats[key] == value:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"admission stat {key!r} did not reach {value}")


def test_lane_key_is_unambiguous_and_run_scoped_without_session():
    assert AsyncSessionLanes.key(
        tenant_id="a:b",
        session_id="c",
        run_id="run-1",
    ) != AsyncSessionLanes.key(
        tenant_id="a",
        session_id="b:c",
        run_id="run-2",
    )
    assert AsyncSessionLanes.key(
        tenant_id="tenant",
        session_id=None,
        run_id="run-1",
    ) != AsyncSessionLanes.key(
        tenant_id="tenant",
        session_id=None,
        run_id="run-2",
    )


@pytest.mark.asyncio
async def test_same_session_serializes_and_different_sessions_overlap():
    lanes = AsyncSessionLanes(max_concurrency=4)
    release = asyncio.Event()
    entered: list[str] = []

    async def work(name: str, session_id: str):
        async with lanes.hold(
            tenant_id="tenant-1",
            session_id=session_id,
            run_id=name,
        ):
            entered.append(name)
            await release.wait()

    first = asyncio.create_task(work("same-1", "same"))
    second = asyncio.create_task(work("same-2", "same"))
    other = asyncio.create_task(work("other", "other"))
    for _ in range(20):
        if len(entered) == 2:
            break
        await asyncio.sleep(0)

    assert set(entered) == {"same-1", "other"}
    assert lanes.active == 2
    release.set()
    await asyncio.gather(first, second, other)
    assert entered.index("same-2") > entered.index("same-1")
    assert lanes.lane_count == 0


@pytest.mark.asyncio
async def test_global_bound_is_enforced_without_hot_session_slot_starvation():
    lanes = AsyncSessionLanes(max_concurrency=2)
    release = asyncio.Event()
    entered = asyncio.Event()

    async def work(index: int):
        async with lanes.hold(
            tenant_id="tenant-1",
            session_id=f"session-{index}",
            run_id=f"run-{index}",
        ):
            if lanes.active == 2:
                entered.set()
            await release.wait()

    tasks = [asyncio.create_task(work(index)) for index in range(5)]
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert lanes.active == 2
    assert lanes.peak_active == 2
    release.set()
    await asyncio.gather(*tasks)
    assert lanes.lane_count == 0


@pytest.mark.asyncio
async def test_waiter_cancellation_releases_lane_reference():
    lanes = AsyncSessionLanes(max_concurrency=1)
    release = asyncio.Event()

    async def work(run_id: str):
        async with lanes.hold(
            tenant_id="tenant-1",
            session_id="same",
            run_id=run_id,
        ):
            await release.wait()

    holder = asyncio.create_task(work("holder"))
    while lanes.active == 0:
        await asyncio.sleep(0)
    waiter = asyncio.create_task(work("waiter"))
    await _wait_for_stat(lanes, "waiting", 1)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release.set()
    await holder

    assert lanes.active == 0
    assert lanes.lane_count == 0
    assert lanes.admission_stats["cancelled"] == 1


@pytest.mark.asyncio
async def test_ready_sessions_are_admitted_in_tenant_round_robin_order():
    lanes = AsyncSessionLanes(
        max_concurrency=1,
        max_waiting=8,
        admission_timeout_seconds=None,
    )
    entered: list[str] = []
    releases = {name: asyncio.Event() for name in ("holder", "hot-1", "hot-2", "cool")}

    async def work(name: str, tenant_id: str) -> None:
        async with lanes.hold(
            tenant_id=tenant_id,
            session_id=name,
            run_id=name,
        ):
            entered.append(name)
            await releases[name].wait()

    holder = asyncio.create_task(work("holder", "hot"))
    while entered != ["holder"]:
        await asyncio.sleep(0)
    hot_1 = asyncio.create_task(work("hot-1", "hot"))
    hot_2 = asyncio.create_task(work("hot-2", "hot"))
    await _wait_for_stat(lanes, "waiting", 2)
    cool = asyncio.create_task(work("cool", "cool"))
    await _wait_for_stat(lanes, "waiting", 3)

    releases["holder"].set()
    while len(entered) < 2:
        await asyncio.sleep(0)
    assert entered == ["holder", "hot-1"]
    releases["hot-1"].set()
    while len(entered) < 3:
        await asyncio.sleep(0)
    assert entered == ["holder", "hot-1", "cool"]
    releases["cool"].set()
    while len(entered) < 4:
        await asyncio.sleep(0)
    assert entered == ["holder", "hot-1", "cool", "hot-2"]
    releases["hot-2"].set()

    await asyncio.gather(holder, hot_1, hot_2, cool)
    stats = lanes.admission_stats
    assert stats["fair_queue_admissions"] == 3
    assert stats["active"] == stats["waiting"] == stats["lane_count"] == 0


@pytest.mark.asyncio
async def test_bounded_waiting_rejects_excess_with_typed_retry_hint():
    lanes = AsyncSessionLanes(
        max_concurrency=1,
        max_waiting=1,
        admission_timeout_seconds=None,
    )
    release = asyncio.Event()

    async def hold(run_id: str) -> None:
        async with lanes.hold(
            tenant_id="tenant-1",
            session_id=run_id,
            run_id=run_id,
        ):
            await release.wait()

    holder = asyncio.create_task(hold("holder"))
    while lanes.active == 0:
        await asyncio.sleep(0)
    waiter = asyncio.create_task(hold("waiter"))
    await _wait_for_stat(lanes, "waiting", 1)

    with pytest.raises(RuntimeAdmissionOverloadedError) as overloaded:
        async with lanes.hold(
            tenant_id="tenant-2",
            session_id="excess",
            run_id="excess",
        ):
            raise AssertionError("excess admission must not execute")
    assert overloaded.value.code == "RUNTIME_ADMISSION_OVERLOADED"
    assert overloaded.value.retryable is True
    assert overloaded.value.details == {
        "reason": "queue_full",
        "retry_after_seconds": 1.0,
        "max_waiting": 1,
        "queue_limit": 1,
    }

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release.set()
    await holder
    assert lanes.admission_stats["rejected"] == 1


@pytest.mark.asyncio
async def test_admission_timeout_cleans_queue_and_session_lane():
    lanes = AsyncSessionLanes(
        max_concurrency=1,
        max_waiting=2,
        admission_timeout_seconds=0.02,
    )
    release = asyncio.Event()

    async def holder() -> None:
        async with lanes.hold(
            tenant_id="tenant-1",
            session_id="holder",
            run_id="holder",
        ):
            await release.wait()

    active = asyncio.create_task(holder())
    while lanes.active == 0:
        await asyncio.sleep(0)
    with pytest.raises(RuntimeAdmissionOverloadedError) as overloaded:
        async with lanes.hold(
            tenant_id="tenant-2",
            session_id="waiter",
            run_id="waiter",
        ):
            raise AssertionError("timed-out admission must not execute")
    assert overloaded.value.details is not None
    assert overloaded.value.details["reason"] == "admission_timeout"
    assert lanes.admission_stats["timed_out"] == 1
    assert lanes.admission_stats["waiting"] == 0

    release.set()
    await active
    assert lanes.lane_count == 0


@pytest.mark.asyncio
async def test_hot_session_cannot_consume_another_tenants_waiting_budget():
    lanes = AsyncSessionLanes(
        max_concurrency=1,
        max_waiting=4,
        max_waiting_per_tenant=3,
        max_waiting_per_session=2,
        admission_timeout_seconds=None,
    )
    holder_release = asyncio.Event()
    cool_entered = asyncio.Event()

    async def holder() -> None:
        async with lanes.hold(
            tenant_id="hot",
            session_id="hot-session",
            run_id="holder",
        ):
            await holder_release.wait()

    async def hot_waiter(run_id: str) -> None:
        async with lanes.hold(
            tenant_id="hot",
            session_id="hot-session",
            run_id=run_id,
        ):
            return

    async def cool_waiter() -> None:
        async with lanes.hold(
            tenant_id="cool",
            session_id="cool-session",
            run_id="cool",
        ):
            cool_entered.set()

    active = asyncio.create_task(holder())
    while lanes.active == 0:
        await asyncio.sleep(0)
    hot_1 = asyncio.create_task(hot_waiter("hot-1"))
    hot_2 = asyncio.create_task(hot_waiter("hot-2"))
    await _wait_for_stat(lanes, "waiting", 2)

    with pytest.raises(RuntimeAdmissionOverloadedError) as overloaded:
        await hot_waiter("hot-excess")
    assert overloaded.value.details is not None
    assert overloaded.value.details["reason"] == "session_queue_full"
    assert overloaded.value.details["queue_limit"] == 2

    cool = asyncio.create_task(cool_waiter())
    await _wait_for_stat(lanes, "waiting", 3)
    assert lanes.admission_stats["waiting_tenants"] == 2
    assert lanes.admission_stats["waiting_sessions"] == 2
    holder_release.set()
    await asyncio.wait_for(cool_entered.wait(), timeout=1)
    await asyncio.gather(active, hot_1, hot_2, cool)
    assert lanes.admission_stats["waiting"] == 0


@pytest.mark.asyncio
async def test_hot_tenant_cannot_fill_global_queue_with_distinct_sessions():
    lanes = AsyncSessionLanes(
        max_concurrency=1,
        max_waiting=4,
        max_waiting_per_tenant=2,
        max_waiting_per_session=2,
        admission_timeout_seconds=None,
    )
    release = asyncio.Event()

    async def work(run_id: str, tenant_id: str) -> None:
        async with lanes.hold(
            tenant_id=tenant_id,
            session_id=run_id,
            run_id=run_id,
        ):
            await release.wait()

    active = asyncio.create_task(work("holder", "hot"))
    while lanes.active == 0:
        await asyncio.sleep(0)
    hot_1 = asyncio.create_task(work("hot-1", "hot"))
    hot_2 = asyncio.create_task(work("hot-2", "hot"))
    await _wait_for_stat(lanes, "waiting", 2)
    with pytest.raises(RuntimeAdmissionOverloadedError) as overloaded:
        await work("hot-excess", "hot")
    assert overloaded.value.details is not None
    assert overloaded.value.details["reason"] == "tenant_queue_full"

    cool = asyncio.create_task(work("cool", "cool"))
    await _wait_for_stat(lanes, "waiting", 3)
    release.set()
    await asyncio.gather(active, hot_1, hot_2, cool)
