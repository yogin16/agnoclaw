#!/usr/bin/env python3
"""Rewind both former writers and prove round-trip PostgreSQL role rotation."""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import sys
import time
from dataclasses import dataclass
from importlib import import_module
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from agnoclaw.runtime import (
    PostgresRuntimeStore,
    RuntimeStoreConnectionLostError,
    RuntimeStoreOverloadedError,
)
from agnoclaw.runtime.lifecycle import (
    LifecycleTransition,
    RunSnapshot,
    RunState,
    TransitionKind,
)
from agnoclaw.runtime.store import RunOwner


def _load_sibling(module_name: str) -> ModuleType:
    """Load one exact probe sibling even when Python isolated mode omits scripts/."""

    if __package__:
        return import_module(f"scripts.{module_name}")
    path = Path(__file__).resolve().with_name(f"{module_name}.py")
    spec = spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load required probe helper {module_name!r}")
    module = module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


base = cast(Any, _load_sibling("postgres_failover_probe"))
sync = cast(Any, _load_sibling("postgres_synchronous_failover_probe"))
if TYPE_CHECKING:
    from scripts.postgres_failover_probe import OwnedResources, TopologyNames

_REPLICATION_NAME_RE = re.compile(r"^(?:primary|standby)_[0-9a-f]{16}$")


@dataclass(frozen=True)
class RotationProbeArguments:
    allow_topology_create: bool
    image: str
    timeout_seconds: float
    outage_timeout_seconds: float


@dataclass(frozen=True)
class RewindResult:
    helper: str
    elapsed_seconds: float
    required_clean_shutdown: bool


def _validate_arguments(args: RotationProbeArguments) -> None:
    base._validate_arguments(
        base.ProbeArguments(
            allow_topology_create=args.allow_topology_create,
            image=args.image,
            timeout_seconds=args.timeout_seconds,
            outage_timeout_seconds=args.outage_timeout_seconds,
        )
    )


def _replication_name(role: str, probe_id: str) -> str:
    name = f"{role}_{probe_id}"
    if not _REPLICATION_NAME_RE.fullmatch(name):
        raise ValueError("replication identity escaped the exact role/probe grammar")
    return name


def _rewind_helper_name(names: TopologyNames, suffix: str) -> str:
    if suffix not in {"a", "b"}:
        raise ValueError("rewind helper suffix must be 'a' or 'b'")
    name = f"{base.RESOURCE_PREFIX}{names.probe_id}-rewind-{suffix}"
    if not base._RESOURCE_RE.fullmatch(name):
        raise AssertionError("rewind helper escaped the owned-resource grammar")
    return name


def _wait_streaming(
    writer_dsn: str,
    application_name: str,
    *,
    timeout: float,
) -> dict[str, object]:
    if not _REPLICATION_NAME_RE.fullmatch(application_name):
        raise ValueError("application name escaped the replication grammar")

    def ready() -> dict[str, object] | None:
        count = int(
            base._query(
                writer_dsn,
                "SELECT count(*) AS count FROM pg_stat_replication "
                "WHERE application_name = %s",
                (application_name,),
            )["count"]
        )
        if count != 1:
            return None
        row = base._query(
            writer_dsn,
            """
            SELECT application_name, state, sync_state,
                   sent_lsn::text AS sent_lsn,
                   write_lsn::text AS write_lsn,
                   flush_lsn::text AS flush_lsn,
                   replay_lsn::text AS replay_lsn
            FROM pg_stat_replication
            WHERE application_name = %s
            """,
            (application_name,),
        )
        return row if row["state"] == "streaming" and row["replay_lsn"] else None

    status = base._wait_for(
        "exact rewound standby streaming state",
        ready,
        timeout=timeout,
    )
    if not isinstance(status, dict):
        raise RuntimeError("rewound standby status returned an invalid shape")
    return status


def _rewind_target(
    *,
    names: TopologyNames,
    owned: OwnedResources,
    helper_suffix: str,
    target_container: str,
    target_volume: str,
    source_container: str,
    source_alias: str,
    replication_name: str,
    image: str,
    password: str,
    require_clean_shutdown: bool,
    timeout: float,
) -> RewindResult:
    """Rewind one exact stopped owned node; never restart a failed target."""
    helper = _rewind_helper_name(names, helper_suffix)
    if target_container not in owned.containers or source_container not in owned.containers:
        raise ValueError("rewind source and target must be owned probe containers")
    if target_volume not in owned.volumes:
        raise ValueError("rewind target must be an owned probe volume")
    if target_container == source_container:
        raise ValueError("rewind source and target containers must differ")
    if source_alias not in {"primary", "standby"}:
        raise ValueError("rewind source alias must be 'primary' or 'standby'")
    if not _REPLICATION_NAME_RE.fullmatch(replication_name):
        raise ValueError("rewind replication identity is invalid")
    if base._container_running(target_container, timeout=timeout):
        raise RuntimeError("refusing to rewind a running target container")
    if not base._container_running(source_container, timeout=timeout):
        raise RuntimeError("refusing to rewind from a stopped source container")

    label = f"io.agnoclaw.postgres-failover-probe={names.probe_id}"
    source_conninfo = (
        f"host={source_alias} port=5432 user=postgres dbname={base.DATABASE} "
        f"application_name={replication_name} connect_timeout=2"
    )
    base._create_resource(
        owned,
        "containers",
        helper,
        "create",
        "--name",
        helper,
        "--label",
        label,
        "--network",
        names.network,
        "--env",
        f"REWIND_PASSWORD={password}",
        "--env",
        f"SOURCE_CONNINFO={source_conninfo}",
        "--env",
        f"REQUIRE_CLEAN={'1' if require_clean_shutdown else '0'}",
        "--volume",
        f"{target_volume}:/var/lib/postgresql/data",
        image,
        "sh",
        "-ceu",
        """
        umask 077
        printf '*:*:*:postgres:%s\n' "$REWIND_PASSWORD" > /tmp/rewind.pgpass
        chown postgres:postgres /tmp/rewind.pgpass
        unset REWIND_PASSWORD
        export PGPASSFILE=/tmp/rewind.pgpass
        trap 'rm -f /tmp/rewind.pgpass' EXIT
        chown -R postgres:postgres "$PGDATA"
        target_state=$(gosu postgres pg_controldata "$PGDATA" \
          | sed -n 's/^[[:space:]]*Database cluster state:[[:space:]]*//p')
        test -n "$target_state"
        if [ "$REQUIRE_CLEAN" = 1 ]; then
          test "$target_state" = 'shut down'
          ensure_option=--no-ensure-shutdown
        else
          ensure_option=
        fi
        gosu postgres pg_rewind \
          --target-pgdata="$PGDATA" \
          --source-server="$SOURCE_CONNINFO" \
          --progress \
          $ensure_option
        touch "$PGDATA/standby.signal"
        sed -i '/^[[:space:]]*primary_conninfo[[:space:]]*=/d' \
          "$PGDATA/postgresql.auto.conf"
        printf "primary_conninfo = '%s'\n" "$SOURCE_CONNINFO" \
          >> "$PGDATA/postgresql.auto.conf"
        chown postgres:postgres \
          "$PGDATA/standby.signal" "$PGDATA/postgresql.auto.conf"
        test -f "$PGDATA/standby.signal"
        test "$(grep -Ec '^[[:space:]]*primary_conninfo[[:space:]]*=' \
          "$PGDATA/postgresql.auto.conf")" = 1
        if grep -Eq '(^|[[:space:]])password=' "$PGDATA/postgresql.auto.conf"; then
          exit 1
        fi
        """,
        timeout=timeout,
    )
    started = time.monotonic()
    base._docker("start", "--attach", helper, timeout=timeout)
    elapsed = time.monotonic() - started
    return RewindResult(
        helper=helper,
        elapsed_seconds=elapsed,
        required_clean_shutdown=require_clean_shutdown,
    )


def _start_rejoined_standby(
    *,
    container: str,
    writer_dsn: str,
    replication_name: str,
    password: str,
    observer_name: str,
    timeout: float,
) -> tuple[str, int, dict[str, object]]:
    base._docker("start", container, timeout=timeout)
    port = base._published_loopback_port(container, timeout=timeout)
    standby_dsn = base._single_dsn(
        port=port,
        password=password,
        application_name=observer_name,
    )
    base._wait_for(
        "rewound standby recovery readiness",
        lambda: bool(base._query(standby_dsn, "SELECT pg_is_in_recovery() AS value")["value"]),
        timeout=timeout,
    )
    status = _wait_streaming(
        writer_dsn,
        replication_name,
        timeout=timeout,
    )
    return standby_dsn, port, status


def _event_manifest(dsn: str, run_id: str) -> tuple[tuple[object, ...], ...]:
    with base._connect(dsn, timeout=2) as conn:
        rows = conn.execute(
            """
            SELECT sequence, event_type, event_json
            FROM runtime_events WHERE run_id = %s ORDER BY sequence
            """,
            (run_id,),
        ).fetchall()
    return tuple((row["sequence"], row["event_type"], row["event_json"]) for row in rows)


def _checkpoint_lsn(writer_dsn: str) -> str:
    """Force one common rewind point and return the exact durable WAL boundary."""
    with base._connect(writer_dsn, timeout=2) as conn:
        conn.execute("CHECKPOINT")
        row = conn.execute(
            "SELECT pg_current_wal_flush_lsn()::text AS lsn"
        ).fetchone()
    if row is None or not isinstance(row["lsn"], str):
        raise RuntimeError("PostgreSQL did not return the post-checkpoint WAL boundary")
    base._lsn_to_int(row["lsn"])
    return str(row["lsn"])


def _runtime_manifest(dsn: str, run_id: str) -> dict[str, object]:
    return base._query(
        dsn,
        """
        SELECT run.state, run.revision,
               (SELECT count(*) FROM runtime_events WHERE run_id = run.run_id) AS events,
               (SELECT count(*) FROM runtime_execution_leases
                WHERE run_id = run.run_id) AS leases,
               (SELECT max(fence_token) FROM runtime_execution_leases
                WHERE run_id = run.run_id) AS max_fence
        FROM runtime_runs AS run WHERE run.run_id = %s
        """,
        (run_id,),
    )


def _wait_pool_run(
    store: PostgresRuntimeStore,
    run_id: str,
    owner: RunOwner,
    *,
    timeout: float,
) -> tuple[RunSnapshot, int]:
    deadline = time.monotonic() + timeout
    attempts = 0
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        attempts += 1
        try:
            return store.get_run(run_id, owner=owner), attempts
        except (RuntimeStoreConnectionLostError, RuntimeStoreOverloadedError) as exc:
            last_error = exc
            time.sleep(0.2)
    raise RuntimeError("pool did not reconnect to the rotated writer") from last_error


def _assert_no_writer(
    store: PostgresRuntimeStore,
    run_id: str,
    owner: RunOwner,
    *,
    outage_timeout: float,
) -> tuple[str, float]:
    started = time.monotonic()
    try:
        store.get_run(run_id, owner=owner)
    except (RuntimeStoreConnectionLostError, RuntimeStoreOverloadedError) as exc:
        elapsed = time.monotonic() - started
        if elapsed > outage_timeout + 2:
            raise RuntimeError(
                "role-rotation no-writer interval exceeded its bound"
            ) from exc
        return exc.code, elapsed
    raise RuntimeError("pool found a writable node before fenced promotion")


def _promote(dsn: str, *, timeout: float) -> float:
    started = time.monotonic()
    promoted = bool(
        base._query(
            dsn,
            "SELECT pg_promote(true, %s) AS promoted",
            (math.ceil(timeout),),
        )["promoted"]
    )
    if not promoted:
        raise RuntimeError("PostgreSQL did not complete role-rotation promotion")
    base._wait_for(
        "role-rotation promotion",
        lambda: not bool(base._query(dsn, "SELECT pg_is_in_recovery() AS value")["value"]),
        timeout=timeout,
    )
    return time.monotonic() - started


def _assert_runtime_remote_apply(store: PostgresRuntimeStore) -> None:
    with store._connection() as conn:  # noqa: SLF001 - transaction-policy oracle
        value = str(
            conn.execute(
                "SELECT current_setting('synchronous_commit') AS value"
            ).fetchone()["value"]
        )
    if value != "remote_apply":
        raise RuntimeError("rotation RuntimeStore connection bypassed remote_apply")


def _probe(
    args: RotationProbeArguments,
    names: TopologyNames,
    owned: OwnedResources,
) -> dict[str, object]:
    password = uuid4().hex
    image_started = time.monotonic()
    image_identity = base._resolve_image(args.image, timeout=args.timeout_seconds)
    image_resolution_seconds = time.monotonic() - image_started
    drill_started = time.monotonic()
    primary_dsn, standby_dsn, primary_port, standby_port = base._create_topology(
        names,
        image=image_identity,
        password=password,
        timeout=args.timeout_seconds,
        owned=owned,
    )
    primary_replication_name = _replication_name("primary", names.probe_id)
    standby_replication_name = _replication_name("standby", names.probe_id)
    rewind_safety = base._query(
        primary_dsn,
        """
        SELECT current_setting('data_checksums') AS data_checksums,
               current_setting('full_page_writes') AS full_page_writes,
               current_setting('wal_log_hints') AS wal_log_hints
        """,
    )
    if rewind_safety["data_checksums"] != "on" or rewind_safety["full_page_writes"] != "on":
        raise RuntimeError("topology does not satisfy pg_rewind durability prerequisites")

    sync._configure_remote_apply(primary_dsn, standby_replication_name)
    sync._wait_synchronous(
        primary_dsn,
        standby_replication_name,
        timeout=args.timeout_seconds,
    )
    owner = RunOwner("agnoclaw-probe", "agnoclaw-probe")
    run_id = f"pg_role_rotation_probe_{names.probe_id}"
    snapshot = RunSnapshot(
        run_id=run_id,
        tenant_id=owner.tenant_id,
        user_id=owner.user_id,
        session_id=f"role-rotation-probe-{names.probe_id}",
    )
    initial_application = f"agnoclaw-rotation-initial-{names.probe_id}"
    initial_multi_dsn = base._multi_host_dsn(
        primary_port=primary_port,
        standby_port=standby_port,
        password=password,
        application_name=initial_application,
    )
    stores: list[PostgresRuntimeStore] = []
    initial_store = PostgresRuntimeStore(
        initial_multi_dsn,
        min_pool_size=1,
        max_pool_size=2,
        max_waiting=2,
        pool_timeout_seconds=args.outage_timeout_seconds,
        connect_timeout_seconds=2,
        application_name=initial_application,
    )
    stores.append(initial_store)
    try:
        _assert_runtime_remote_apply(initial_store)
        created = initial_store.create_run(snapshot)
        if not created.created:
            raise RuntimeError("role-rotation run was not created")
        claim_one = initial_store.acquire_run_lease(
            run_id,
            worker_id="rotation-before-loss",
            claim_id=f"rotation-1:{names.probe_id}",
            lease_seconds=max(120, math.ceil(args.timeout_seconds * 3)),
            owner=owner,
        ).claim
        queued = initial_store.apply_transition(
            LifecycleTransition(
                run_id=run_id,
                kind=TransitionKind.QUEUE,
                transition_id=f"{run_id}:queue",
            ),
            expected_revision=0,
        ).lifecycle.after
        initial_store.release_run_lease(claim_one)
        if queued.state is not RunState.QUEUED:
            raise RuntimeError("initial role-rotation manifest did not queue")
        initial_lsn = _checkpoint_lsn(primary_dsn)
        initial_replay = base._wait_replayed(
            standby_dsn,
            initial_lsn,
            timeout=args.timeout_seconds,
        )

        loss_started = time.monotonic()
        base._docker("kill", "--signal", "KILL", names.primary, timeout=args.timeout_seconds)
        if base._container_running(names.primary, timeout=args.timeout_seconds):
            raise RuntimeError("abruptly killed first primary still reports running")
        first_primary_loss_seconds = time.monotonic() - loss_started
        base._require_safe_promotion(
            primary_running=False,
            replay_lsn=initial_replay,
            acknowledged_lsn=initial_lsn,
        )
        first_no_writer_code, first_no_writer_seconds = _assert_no_writer(
            initial_store,
            run_id,
            owner,
            outage_timeout=args.outage_timeout_seconds,
        )
        first_promotion_seconds = _promote(standby_dsn, timeout=args.timeout_seconds)
        if base._container_running(names.primary, timeout=args.timeout_seconds):
            raise RuntimeError("first old primary lost its fence after promotion")
        recovered_after_first, first_reconnect_attempts = _wait_pool_run(
            initial_store,
            run_id,
            owner,
            timeout=args.timeout_seconds,
        )
        if recovered_after_first.state is not RunState.QUEUED:
            raise RuntimeError("first promotion changed acknowledged state")

        first_rewind = _rewind_target(
            names=names,
            owned=owned,
            helper_suffix="a",
            target_container=names.primary,
            target_volume=names.primary_volume,
            source_container=names.standby,
            source_alias="standby",
            replication_name=primary_replication_name,
            image=image_identity,
            password=password,
            require_clean_shutdown=False,
            timeout=args.timeout_seconds,
        )
        primary_dsn, primary_port, first_rejoin_status = _start_rejoined_standby(
            container=names.primary,
            writer_dsn=standby_dsn,
            replication_name=primary_replication_name,
            password=password,
            observer_name=f"agnoclaw-rejoined-a-{names.probe_id}",
            timeout=args.timeout_seconds,
        )
        first_rejoin_write_sqlstate = base._assert_standby_read_only(primary_dsn)
        sync._configure_remote_apply(standby_dsn, primary_replication_name)
        sync._wait_synchronous(
            standby_dsn,
            primary_replication_name,
            timeout=args.timeout_seconds,
        )

        initial_store.close()
        stores.remove(initial_store)
        rotation_application = f"agnoclaw-rotation-store-{names.probe_id}"
        rotation_multi_dsn = base._multi_host_dsn(
            primary_port=primary_port,
            standby_port=standby_port,
            password=password,
            application_name=rotation_application,
        )
        rotation_store = PostgresRuntimeStore(
            rotation_multi_dsn,
            min_pool_size=1,
            max_pool_size=2,
            max_waiting=2,
            pool_timeout_seconds=args.outage_timeout_seconds,
            connect_timeout_seconds=2,
            application_name=rotation_application,
        )
        stores.append(rotation_store)
        _assert_runtime_remote_apply(rotation_store)
        running = rotation_store.apply_transition(
            LifecycleTransition(
                run_id=run_id,
                kind=TransitionKind.START,
                transition_id=f"{run_id}:start-on-second-writer",
            ),
            expected_revision=1,
        ).lifecycle.after
        claim_two = rotation_store.acquire_run_lease(
            run_id,
            worker_id="rotation-on-second-writer",
            claim_id=f"rotation-2:{names.probe_id}",
            lease_seconds=max(120, math.ceil(args.timeout_seconds * 2)),
            owner=owner,
        ).claim
        rotation_store.release_run_lease(claim_two)
        if running.state is not RunState.RUNNING:
            raise RuntimeError("second writer did not commit running state")
        if claim_two.run.fence_token <= claim_one.run.fence_token:
            raise RuntimeError("lease fence did not advance after first rejoin")
        second_writer_lsn = _checkpoint_lsn(standby_dsn)
        replay_on_a = base._wait_replayed(
            primary_dsn,
            second_writer_lsn,
            timeout=args.timeout_seconds,
        )

        base._docker("stop", "--time", "5", names.standby, timeout=args.timeout_seconds)
        if base._container_running(names.standby, timeout=args.timeout_seconds):
            raise RuntimeError("second writer remained running before return promotion")
        base._require_safe_promotion(
            primary_running=False,
            replay_lsn=replay_on_a,
            acknowledged_lsn=second_writer_lsn,
        )
        second_no_writer_code, second_no_writer_seconds = _assert_no_writer(
            rotation_store,
            run_id,
            owner,
            outage_timeout=args.outage_timeout_seconds,
        )
        second_promotion_seconds = _promote(primary_dsn, timeout=args.timeout_seconds)
        if base._container_running(names.standby, timeout=args.timeout_seconds):
            raise RuntimeError("second old primary lost its fence after return promotion")
        recovered_after_second, second_reconnect_attempts = _wait_pool_run(
            rotation_store,
            run_id,
            owner,
            timeout=args.timeout_seconds,
        )
        if recovered_after_second.state is not RunState.RUNNING:
            raise RuntimeError("return promotion changed acknowledged state")

        second_rewind = _rewind_target(
            names=names,
            owned=owned,
            helper_suffix="b",
            target_container=names.standby,
            target_volume=names.standby_volume,
            source_container=names.primary,
            source_alias="primary",
            replication_name=standby_replication_name,
            image=image_identity,
            password=password,
            require_clean_shutdown=True,
            timeout=args.timeout_seconds,
        )
        standby_dsn, standby_port, second_rejoin_status = _start_rejoined_standby(
            container=names.standby,
            writer_dsn=primary_dsn,
            replication_name=standby_replication_name,
            password=password,
            observer_name=f"agnoclaw-rejoined-b-{names.probe_id}",
            timeout=args.timeout_seconds,
        )
        second_rejoin_write_sqlstate = base._assert_standby_read_only(standby_dsn)
        sync._configure_remote_apply(primary_dsn, standby_replication_name)
        sync._wait_synchronous(
            primary_dsn,
            standby_replication_name,
            timeout=args.timeout_seconds,
        )
        _assert_runtime_remote_apply(rotation_store)
        paused = rotation_store.apply_transition(
            LifecycleTransition(
                run_id=run_id,
                kind=TransitionKind.PAUSE,
                transition_id=f"{run_id}:pause-after-return",
            ),
            expected_revision=2,
        ).lifecycle.after
        claim_three = rotation_store.acquire_run_lease(
            run_id,
            worker_id="rotation-after-return",
            claim_id=f"rotation-3:{names.probe_id}",
            lease_seconds=120,
            owner=owner,
        ).claim
        rotation_store.release_run_lease(claim_three)
        if paused.state is not RunState.PAUSED or paused.revision != 3:
            raise RuntimeError("returned writer did not commit the final state")
        if claim_three.run.fence_token <= claim_two.run.fence_token:
            raise RuntimeError("lease fence did not advance after return rotation")
        final_lsn = str(
            base._query(primary_dsn, "SELECT pg_current_wal_flush_lsn()::text AS lsn")["lsn"]
        )
        final_replay = base._wait_replayed(
            standby_dsn,
            final_lsn,
            timeout=args.timeout_seconds,
        )
        writer_manifest = _runtime_manifest(primary_dsn, run_id)
        standby_manifest = _runtime_manifest(standby_dsn, run_id)
        if writer_manifest != standby_manifest:
            raise RuntimeError("runtime manifest diverged after round-trip rotation")
        writer_events = _event_manifest(primary_dsn, run_id)
        standby_events = _event_manifest(standby_dsn, run_id)
        if writer_events != standby_events:
            raise RuntimeError("event manifest diverged after round-trip rotation")
        if [row[0] for row in writer_events] != list(range(1, len(writer_events) + 1)):
            raise RuntimeError("event sequence is not contiguous after round-trip rotation")

        with rotation_store._connection() as conn:  # noqa: SLF001 - routing oracle
            current_node = str(
                conn.execute("SELECT current_setting('cluster_name') AS value").fetchone()[
                    "value"
                ]
            )
            connection_count = int(
                conn.execute(
                    "SELECT count(*) AS count FROM pg_stat_activity "
                    "WHERE application_name = %s",
                    (rotation_application,),
                ).fetchone()["count"]
            )
        if current_node != "agnoclaw_failover_primary":
            raise RuntimeError("pool did not return to the original node as writer")
        if connection_count > 2:
            raise RuntimeError("rotation probe exceeded its declared connection bound")

        final_multi_dsn = base._multi_host_dsn(
            primary_port=primary_port,
            standby_port=standby_port,
            password=password,
            application_name=f"agnoclaw-rotation-fresh-{names.probe_id}",
        )
        with PostgresRuntimeStore(
            final_multi_dsn,
            min_pool_size=1,
            max_pool_size=1,
            application_name=f"agnoclaw-rotation-fresh-{names.probe_id}",
        ) as fresh:
            durable = fresh.get_run(run_id, owner=owner)
            if durable.state is not RunState.PAUSED or durable.revision != 3:
                raise RuntimeError("fresh pool cannot observe final rotated state")

        return {
            "status": "passed",
            "scope": "owned_two_node_double_rewind_round_trip_role_rotation_probe",
            "production_certification": False,
            "postgres_image": args.image,
            "postgres_image_digest": image_identity,
            "postgres_server_version": str(
                base._query(primary_dsn, "SHOW server_version")["server_version"]
            ),
            "data_checksums": rewind_safety["data_checksums"],
            "full_page_writes": rewind_safety["full_page_writes"],
            "wal_log_hints": rewind_safety["wal_log_hints"],
            "first_primary_abrupt_sigkill": True,
            "first_primary_loss_seconds": round(first_primary_loss_seconds, 3),
            "first_no_writer_error": first_no_writer_code,
            "first_no_writer_seconds": round(first_no_writer_seconds, 3),
            "first_promotion_seconds": round(first_promotion_seconds, 3),
            "first_reconnect_attempts": first_reconnect_attempts,
            "first_rewind_seconds": round(first_rewind.elapsed_seconds, 3),
            "first_rewind_automatic_crash_recovery_allowed": True,
            "first_rejoin_state": first_rejoin_status["state"],
            "first_rejoin_read_only_sqlstate": first_rejoin_write_sqlstate,
            "second_writer_remote_apply": True,
            "second_no_writer_error": second_no_writer_code,
            "second_no_writer_seconds": round(second_no_writer_seconds, 3),
            "second_promotion_seconds": round(second_promotion_seconds, 3),
            "second_reconnect_attempts": second_reconnect_attempts,
            "second_rewind_seconds": round(second_rewind.elapsed_seconds, 3),
            "second_rewind_required_clean_shutdown": True,
            "second_rejoin_state": second_rejoin_status["state"],
            "second_rejoin_read_only_sqlstate": second_rejoin_write_sqlstate,
            "writer_roles_observed": [
                "agnoclaw_failover_primary",
                "agnoclaw_failover_standby",
                "agnoclaw_failover_primary",
            ],
            "both_former_writers_rejoined": True,
            "final_writer": current_node,
            "final_standby_streaming": True,
            "final_standby_sync": True,
            "final_acknowledged_lsn_replayed": final_replay,
            "final_state": writer_manifest["state"],
            "final_revision": writer_manifest["revision"],
            "final_event_count": writer_manifest["events"],
            "event_sequence_contiguous": True,
            "run_fence_monotonic": True,
            "final_run_fence": claim_three.run.fence_token,
            "observed_acknowledged_state_loss": 0,
            "observed_application_connections": connection_count,
            "connection_bound": 2,
            "existing_pool_reconnected_across_both_promotions": True,
            "fresh_pool_verified": True,
            "image_resolution_seconds": round(image_resolution_seconds, 3),
            "topology_and_drill_seconds": round(time.monotonic() - drill_started, 3),
            "external_fencing_required": True,
            "automatic_failover_certification": False,
            "split_brain_certification": False,
            "failed_rewind_requires_fresh_base_backup": True,
            "open_production_gates": [
                "deployment-specific automatic failover and external fencing",
                "true network split-brain and witness/quorum behavior",
                "simultaneous and multiple storage/process failures",
                "production endpoint discovery and pool reconfiguration",
                "production synchronous latency, availability, and RPO SLO",
                "encrypted off-host backup, PITR, artifact, and key recovery",
            ],
        }
    finally:
        for store in reversed(stores):
            store.close()


def _arguments() -> RotationProbeArguments:
    parser = argparse.ArgumentParser(
        description=(
            "Create an owned PostgreSQL 17 pair, fail over, rewind/rejoin both former "
            "writers, rotate roles back, and verify exact acknowledged state."
        )
    )
    parser.add_argument("--allow-topology-create", action="store_true")
    parser.add_argument("--image", default=base.POSTGRES_IMAGE)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--outage-timeout", type=float, default=1.0)
    parsed = parser.parse_args()
    return RotationProbeArguments(
        allow_topology_create=bool(parsed.allow_topology_create),
        image=str(parsed.image),
        timeout_seconds=float(parsed.timeout),
        outage_timeout_seconds=float(parsed.outage_timeout),
    )


def main() -> int:
    args = _arguments()
    try:
        _validate_arguments(args)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    names = base._resource_names(uuid4().hex[:16])
    owned = base.OwnedResources()
    output: dict[str, object] | None = None
    primary_failure: BaseException | None = None
    started = time.monotonic()
    pool_logger = logging.getLogger("psycopg.pool")
    prior_pool_level = pool_logger.level
    pool_logger.setLevel(logging.CRITICAL)
    try:
        output = _probe(args, names, owned)
    except BaseException as exc:
        primary_failure = exc
    finally:
        cleanup = base._cleanup_topology(owned, timeout=args.timeout_seconds)
        pool_logger.setLevel(prior_pool_level)
    if primary_failure is not None:
        if cleanup.failures:
            primary_failure.add_note("; ".join(cleanup.failures))
        raise primary_failure.with_traceback(primary_failure.__traceback__)
    if cleanup.failures:
        raise RuntimeError(
            "PostgreSQL role-rotation cleanup failed: " + "; ".join(cleanup.failures)
        )
    if output is None:
        raise AssertionError("PostgreSQL role-rotation probe returned no result")
    output["owned_resources_created"] = (
        len(owned.containers) + len(owned.volumes) + len(owned.networks)
    )
    output["owned_resources_removed"] = len(cleanup.resources_removed)
    output["cleanup_complete"] = True
    output["elapsed_seconds"] = round(time.monotonic() - started, 3)
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
