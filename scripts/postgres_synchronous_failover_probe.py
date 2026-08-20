#!/usr/bin/env python3
"""Prove remote-apply acknowledgement and abrupt-loss promotion on an owned PG pair."""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import sys
import threading
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
from agnoclaw.runtime.store import CreateRunDecision, RunOwner


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
if TYPE_CHECKING:
    from scripts.postgres_failover_probe import OwnedResources, TopologyNames

_STANDBY_NAME_RE = re.compile(r"^(?:primary|standby)_[0-9a-f]{16}$")


@dataclass(frozen=True)
class SynchronousProbeArguments:
    allow_topology_create: bool
    image: str
    timeout_seconds: float
    outage_timeout_seconds: float
    blocked_observation_seconds: float


@dataclass(frozen=True)
class BlockedCommitResult:
    decision: CreateRunDecision
    blocked_observation_seconds: float
    acknowledgement_seconds: float
    standby_reconnect_seconds: float


def _validate_arguments(args: SynchronousProbeArguments) -> None:
    base._validate_arguments(
        base.ProbeArguments(
            allow_topology_create=args.allow_topology_create,
            image=args.image,
            timeout_seconds=args.timeout_seconds,
            outage_timeout_seconds=args.outage_timeout_seconds,
        )
    )
    if not 0.1 <= args.blocked_observation_seconds <= 5:
        raise ValueError("--blocked-observation must be between 0.1 and 5 seconds")
    if args.blocked_observation_seconds >= args.timeout_seconds:
        raise ValueError("--blocked-observation must be less than --timeout")


def _standby_name(probe_id: str) -> str:
    name = f"standby_{probe_id}"
    if not _STANDBY_NAME_RE.fullmatch(name):
        raise ValueError("probe_id must be exactly 16 lowercase hexadecimal characters")
    return name


def _configure_remote_apply(primary_dsn: str, standby_name: str) -> None:
    if not _STANDBY_NAME_RE.fullmatch(standby_name):
        raise ValueError("standby name escaped the synchronous-replication grammar")
    with base._connect(primary_dsn, timeout=2) as conn:
        conn.execute(
            "ALTER SYSTEM SET synchronous_standby_names TO "
            f"'FIRST 1 ({standby_name})'"
        )
        conn.execute("ALTER SYSTEM SET synchronous_commit TO 'remote_apply'")
        row = conn.execute(
            "SELECT pg_reload_conf() AS reloaded"
        ).fetchone()
    if row is None or not row["reloaded"]:
        raise RuntimeError("PostgreSQL did not reload synchronous-replication settings")


def _synchronous_status(primary_dsn: str, standby_name: str) -> dict[str, object] | None:
    row = base._query(
        primary_dsn,
        """
        SELECT application_name, state, sync_state,
               sent_lsn::text AS sent_lsn,
               write_lsn::text AS write_lsn,
               flush_lsn::text AS flush_lsn,
               replay_lsn::text AS replay_lsn
        FROM pg_stat_replication
        WHERE application_name = %s
        """,
        (standby_name,),
    ) if int(
        base._query(
            primary_dsn,
            "SELECT count(*) AS count FROM pg_stat_replication WHERE application_name = %s",
            (standby_name,),
        )["count"]
    ) == 1 else None
    return row


def _wait_synchronous(primary_dsn: str, standby_name: str, *, timeout: float) -> dict[str, object]:
    def ready() -> dict[str, object] | None:
        row = _synchronous_status(primary_dsn, standby_name)
        if row is None:
            return None
        if row["state"] != "streaming" or row["sync_state"] != "sync":
            return None
        if row["replay_lsn"] is None:
            return None
        return row

    status = base._wait_for(
        "synchronous standby streaming state",
        ready,
        timeout=timeout,
    )
    if not isinstance(status, dict):
        raise RuntimeError("synchronous standby status returned an invalid shape")
    return status


def _wait_replica_disconnected(
    primary_dsn: str,
    standby_name: str,
    *,
    timeout: float,
) -> None:
    base._wait_for(
        "primary to observe standby disconnection",
        lambda: int(
            base._query(
                primary_dsn,
                "SELECT count(*) AS count FROM pg_stat_replication WHERE application_name = %s",
                (standby_name,),
            )["count"]
        )
        == 0,
        timeout=timeout,
    )


def _terminate_exact_replication_sender(
    primary_dsn: str,
    standby_name: str,
) -> None:
    """Make an already-partitioned standby disappear without TCP timeout noise."""
    if not _STANDBY_NAME_RE.fullmatch(standby_name):
        raise ValueError("standby name escaped the replication-sender grammar")
    result = base._query(
        primary_dsn,
        """
        SELECT count(*) AS matched,
               COALESCE(bool_and(pg_terminate_backend(pid)), false) AS terminated
        FROM pg_stat_replication
        WHERE application_name = %s
        """,
        (standby_name,),
    )
    if result != {"matched": 1, "terminated": True}:
        raise RuntimeError("exact synchronous replication sender was not terminated")


def _network_connected(container: str, network: str, *, timeout: float) -> bool:
    if not base._RESOURCE_RE.fullmatch(container):
        raise ValueError("container escaped the owned failover-resource grammar")
    if not base._RESOURCE_RE.fullmatch(network):
        raise ValueError("network escaped the owned failover-resource grammar")
    row = base._docker(
        "inspect",
        "--format",
        "{{json .NetworkSettings.Networks}}",
        container,
        timeout=timeout,
    )
    try:
        networks = json.loads(row.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Docker returned invalid container-network state") from exc
    if not isinstance(networks, dict):
        raise RuntimeError("Docker returned an invalid container-network shape")
    return network in networks


def _prove_blocked_runtime_acknowledgement(
    *,
    store: PostgresRuntimeStore,
    snapshot: RunSnapshot,
    standby_container: str,
    standby_network: str,
    standby_dsn: str,
    primary_container: str,
    primary_dsn: str,
    standby_name: str,
    blocked_observation_seconds: float,
    timeout: float,
) -> BlockedCommitResult:
    """Partition the only sync standby; create_run must wait until it reconnects."""
    completed = threading.Event()
    outcome: list[CreateRunDecision | BaseException] = []

    def create() -> None:
        try:
            outcome.append(store.create_run(snapshot))
        except BaseException as exc:  # noqa: BLE001 - passed back to the main thread
            outcome.append(exc)
        finally:
            completed.set()

    base._docker(
        "network",
        "disconnect",
        standby_network,
        standby_container,
        timeout=timeout,
    )
    if _network_connected(standby_container, standby_network, timeout=timeout):
        raise RuntimeError("standby remained attached to the replication network")
    if not base._container_running(standby_container, timeout=timeout):
        raise RuntimeError("replication partition unexpectedly stopped the standby")
    _terminate_exact_replication_sender(primary_dsn, standby_name)
    _wait_replica_disconnected(primary_dsn, standby_name, timeout=timeout)

    started = time.monotonic()
    worker = threading.Thread(
        target=create,
        name="agnoclaw-sync-ack-probe",
        daemon=True,
    )
    worker.start()
    acknowledged_while_disconnected = completed.wait(blocked_observation_seconds)
    observed = time.monotonic() - started
    if acknowledged_while_disconnected:
        if outcome and isinstance(outcome[0], BaseException):
            raise RuntimeError(
                "runtime commit failed instead of waiting for the synchronous standby"
            ) from outcome[0]
        raise RuntimeError("runtime commit acknowledged while the synchronous standby was absent")

    reconnect_started = time.monotonic()
    reconnect_error: BaseException | None = None
    try:
        base._docker(
            "network",
            "connect",
            "--alias",
            "standby",
            standby_network,
            standby_container,
            timeout=timeout,
        )
        if not _network_connected(standby_container, standby_network, timeout=timeout):
            raise RuntimeError("standby did not reattach to the replication network")
        if not bool(
            base._query(
                standby_dsn,
                "SELECT pg_is_in_recovery() AS value",
            )["value"]
        ):
            raise RuntimeError("reconnected standby left recovery before promotion")
        _wait_synchronous(primary_dsn, standby_name, timeout=timeout)
    except BaseException as exc:  # noqa: BLE001 - unblock the database worker below
        reconnect_error = exc
        try:
            base._docker("kill", "--signal", "KILL", primary_container, timeout=timeout)
        except BaseException:
            pass
    standby_reconnect_seconds = time.monotonic() - reconnect_started
    worker.join(timeout=timeout)
    if worker.is_alive():
        try:
            base._docker("kill", "--signal", "KILL", primary_container, timeout=timeout)
        finally:
            worker.join(timeout=5)
        raise RuntimeError("blocked runtime commit did not finish after standby recovery")
    if reconnect_error is not None:
        raise RuntimeError("synchronous standby did not reconnect") from reconnect_error
    if len(outcome) != 1:
        raise RuntimeError("runtime acknowledgement worker produced no single outcome")
    result = outcome[0]
    if isinstance(result, BaseException):
        raise RuntimeError("runtime commit failed after synchronous standby recovery") from result
    return BlockedCommitResult(
        decision=result,
        blocked_observation_seconds=observed,
        acknowledgement_seconds=time.monotonic() - started,
        standby_reconnect_seconds=standby_reconnect_seconds,
    )


def _probe(
    args: SynchronousProbeArguments,
    names: TopologyNames,
    owned: OwnedResources,
) -> dict[str, object]:
    image_started = time.monotonic()
    password = uuid4().hex
    image_identity = base._resolve_image(args.image, timeout=args.timeout_seconds)
    image_resolution_seconds = time.monotonic() - image_started
    topology_started = time.monotonic()
    primary_dsn, standby_dsn, primary_port, standby_port = base._create_topology(
        names,
        image=image_identity,
        password=password,
        timeout=args.timeout_seconds,
        owned=owned,
    )
    standby_name = _standby_name(names.probe_id)
    _configure_remote_apply(primary_dsn, standby_name)
    sync_status = _wait_synchronous(
        primary_dsn,
        standby_name,
        timeout=args.timeout_seconds,
    )
    settings = base._query(
        primary_dsn,
        """
        SELECT current_setting('synchronous_commit') AS synchronous_commit,
               current_setting('synchronous_standby_names') AS synchronous_standby_names,
               current_setting('cluster_name') AS cluster_name
        """,
    )
    if settings != {
        "synchronous_commit": "remote_apply",
        "synchronous_standby_names": f"FIRST 1 ({standby_name})",
        "cluster_name": "agnoclaw_failover_primary",
    }:
        raise RuntimeError("primary did not apply the exact synchronous settings")

    application_name = f"agnoclaw-sync-store-{names.probe_id}"
    multi_dsn = base._multi_host_dsn(
        primary_port=primary_port,
        standby_port=standby_port,
        password=password,
        application_name=application_name,
    )
    owner = RunOwner("agnoclaw-probe", "agnoclaw-probe")
    run_id = f"pg_sync_failover_probe_{names.probe_id}"
    snapshot = RunSnapshot(
        run_id=run_id,
        tenant_id=owner.tenant_id,
        user_id=owner.user_id,
        session_id=f"sync-failover-probe-{names.probe_id}",
    )
    store = PostgresRuntimeStore(
        multi_dsn,
        min_pool_size=1,
        max_pool_size=2,
        max_waiting=2,
        pool_timeout_seconds=args.outage_timeout_seconds,
        connect_timeout_seconds=2,
        application_name=application_name,
    )
    try:
        with store._connection() as conn:  # noqa: SLF001 - transaction-policy oracle
            runtime_synchronous_commit = str(
                conn.execute(
                    "SELECT current_setting('synchronous_commit') AS value"
                ).fetchone()["value"]
            )
        if runtime_synchronous_commit != "remote_apply":
            raise RuntimeError("runtime pool did not inherit remote-apply acknowledgement")
        blocked = _prove_blocked_runtime_acknowledgement(
            store=store,
            snapshot=snapshot,
            standby_container=names.standby,
            standby_network=names.network,
            standby_dsn=standby_dsn,
            primary_container=names.primary,
            primary_dsn=primary_dsn,
            standby_name=standby_name,
            blocked_observation_seconds=args.blocked_observation_seconds,
            timeout=args.timeout_seconds,
        )
        if not blocked.decision.created or blocked.decision.snapshot.run_id != run_id:
            raise RuntimeError("rejoined synchronous standby released an invalid create decision")
        replicated = base._query(
            standby_dsn,
            "SELECT state, revision FROM runtime_runs WHERE run_id = %s",
            (run_id,),
        )
        if replicated != {"state": "created", "revision": 0}:
            raise RuntimeError("acknowledged runtime creation was not applied on the standby")

        claim = store.acquire_run_lease(
            run_id,
            worker_id="sync-probe-before-abrupt-loss",
            claim_id=f"sync-probe:{names.probe_id}",
            lease_seconds=max(120, math.ceil(args.timeout_seconds * 2)),
            owner=owner,
        ).claim
        queued = store.apply_transition(
            LifecycleTransition(
                run_id=run_id,
                kind=TransitionKind.QUEUE,
                transition_id=f"{run_id}:queue-before-abrupt-loss",
            ),
            expected_revision=0,
        ).lifecycle.after
        store.release_run_lease(claim)
        if queued.state is not RunState.QUEUED:
            raise RuntimeError("pre-loss acknowledged transition did not commit")
        primary_events = store.list_events(run_id, owner=owner)
        if [event.sequence for event in primary_events] != list(
            range(1, len(primary_events) + 1)
        ):
            raise RuntimeError("pre-loss event sequence is not contiguous")
        acknowledged_lsn = str(
            base._query(
                primary_dsn,
                "SELECT pg_current_wal_flush_lsn()::text AS lsn",
            )["lsn"]
        )
        replay_lsn = base._wait_replayed(
            standby_dsn,
            acknowledged_lsn,
            timeout=args.timeout_seconds,
        )
        before_loss = base._query(
            standby_dsn,
            """
            SELECT run.state, run.revision,
                   (SELECT count(*) FROM runtime_events WHERE run_id = run.run_id) AS events,
                   (SELECT count(*) FROM runtime_execution_leases
                    WHERE run_id = run.run_id) AS leases,
                   (SELECT min(fence_token) FROM runtime_execution_leases
                    WHERE run_id = run.run_id) AS min_fence,
                   (SELECT max(fence_token) FROM runtime_execution_leases
                    WHERE run_id = run.run_id) AS max_fence
            FROM runtime_runs AS run WHERE run.run_id = %s
            """,
            (run_id,),
        )
        expected_manifest = {
            "state": "queued",
            "revision": 1,
            "events": len(primary_events),
            "leases": 2,
            "min_fence": claim.run.fence_token,
            "max_fence": claim.run.fence_token,
        }
        if before_loss != expected_manifest:
            raise RuntimeError("standby did not expose the exact acknowledged runtime manifest")

        loss_started = time.monotonic()
        base._docker("kill", "--signal", "KILL", names.primary, timeout=args.timeout_seconds)
        if base._container_running(names.primary, timeout=args.timeout_seconds):
            raise RuntimeError("abruptly killed primary still reports running")
        primary_loss_seconds = time.monotonic() - loss_started
        base._require_safe_promotion(
            primary_running=False,
            replay_lsn=replay_lsn,
            acknowledged_lsn=acknowledged_lsn,
        )

        no_writer_started = time.monotonic()
        try:
            store.get_run(run_id, owner=owner)
        except (RuntimeStoreConnectionLostError, RuntimeStoreOverloadedError) as exc:
            no_writer_error = exc
        else:
            raise RuntimeError("pool found a writable node before abrupt-loss promotion")
        no_writer_failure_seconds = time.monotonic() - no_writer_started
        if no_writer_failure_seconds > args.outage_timeout_seconds + 2:
            raise RuntimeError("abrupt-loss no-writer interval exceeded the bounded window")

        promotion_started = time.monotonic()
        promoted = bool(
            base._query(
                standby_dsn,
                "SELECT pg_promote(true, %s) AS promoted",
                (math.ceil(args.timeout_seconds),),
            )["promoted"]
        )
        if not promoted:
            raise RuntimeError("PostgreSQL did not complete abrupt-loss promotion")
        base._wait_for(
            "abrupt-loss standby promotion",
            lambda: not bool(
                base._query(
                    standby_dsn,
                    "SELECT pg_is_in_recovery() AS value",
                )["value"]
            ),
            timeout=args.timeout_seconds,
        )
        promotion_seconds = time.monotonic() - promotion_started
        if base._container_running(names.primary, timeout=args.timeout_seconds):
            raise RuntimeError("old primary lost its fence after abrupt-loss promotion")

        deadline = time.monotonic() + args.timeout_seconds
        reconnect_attempts = 0
        last_error: BaseException | None = None
        while time.monotonic() < deadline:
            reconnect_attempts += 1
            try:
                recovered = store.get_run(run_id, owner=owner)
                break
            except (RuntimeStoreConnectionLostError, RuntimeStoreOverloadedError) as exc:
                last_error = exc
                time.sleep(0.2)
        else:
            raise RuntimeError("existing pool did not reach the promoted writer") from last_error
        recovered_events = store.list_events(run_id, owner=owner)
        if recovered.state is not RunState.QUEUED or recovered.revision != 1:
            raise RuntimeError("acknowledged state was lost after abrupt primary failure")
        if [event.to_dict() for event in recovered_events] != [
            event.to_dict() for event in primary_events
        ]:
            raise RuntimeError("acknowledged event manifest changed after abrupt loss")
        after_loss = base._query(
            standby_dsn,
            """
            SELECT run.state, run.revision,
                   (SELECT count(*) FROM runtime_events WHERE run_id = run.run_id) AS events,
                   (SELECT count(*) FROM runtime_execution_leases
                    WHERE run_id = run.run_id) AS leases,
                   (SELECT min(fence_token) FROM runtime_execution_leases
                    WHERE run_id = run.run_id) AS min_fence,
                   (SELECT max(fence_token) FROM runtime_execution_leases
                    WHERE run_id = run.run_id) AS max_fence
            FROM runtime_runs AS run WHERE run.run_id = %s
            """,
            (run_id,),
        )
        if after_loss != expected_manifest:
            raise RuntimeError("runtime manifest changed across abrupt primary loss")

        with store._connection() as conn:  # noqa: SLF001 - bounded connection oracle
            connection_count = int(
                conn.execute(
                    """
                    SELECT count(*) AS count FROM pg_stat_activity
                    WHERE application_name = %s
                    """,
                    (application_name,),
                ).fetchone()["count"]
            )
        if connection_count > 2:
            raise RuntimeError("synchronous probe exceeded its declared connection bound")

        with PostgresRuntimeStore(
            multi_dsn,
            min_pool_size=1,
            max_pool_size=1,
            application_name=f"agnoclaw-sync-fresh-{names.probe_id}",
        ) as reopened:
            durable = reopened.get_run(run_id, owner=owner)
            if durable.state is not RunState.QUEUED:
                raise RuntimeError("fresh pool cannot observe abrupt-loss recovery state")

        return {
            "status": "passed",
            "scope": "owned_two_node_remote_apply_abrupt_loss_probe",
            "production_certification": False,
            "postgres_image": args.image,
            "postgres_image_digest": image_identity,
            "postgres_server_version": str(
                base._query(standby_dsn, "SHOW server_version")["server_version"]
            ),
            "image_resolution_seconds": round(image_resolution_seconds, 3),
            "topology_and_drill_seconds": round(
                time.monotonic() - topology_started,
                3,
            ),
            "synchronous_commit": "remote_apply",
            "runtime_connection_synchronous_commit": runtime_synchronous_commit,
            "synchronous_standby_names": f"FIRST 1 ({standby_name})",
            "initial_replication_state": sync_status["state"],
            "initial_sync_state": sync_status["sync_state"],
            "standby_replication_partition_prevented_false_ack": True,
            "exact_replication_sender_terminated_after_partition": True,
            "blocked_observation_seconds": round(
                blocked.blocked_observation_seconds,
                3,
            ),
            "acknowledgement_after_rejoin_seconds": round(
                blocked.acknowledgement_seconds,
                3,
            ),
            "standby_reconnect_seconds": round(blocked.standby_reconnect_seconds, 3),
            "acknowledged_manifest_applied_before_loss": True,
            "acknowledged_lsn_replayed_before_loss": True,
            "abrupt_primary_sigkill": True,
            "primary_loss_seconds": round(primary_loss_seconds, 3),
            "old_primary_fenced_before_promotion": True,
            "old_primary_fence_preserved_after_promotion": True,
            "no_writer_error": no_writer_error.code,
            "no_writer_failure_seconds": round(no_writer_failure_seconds, 3),
            "promotion_seconds": round(promotion_seconds, 3),
            "existing_pool_reconnected": True,
            "fresh_pool_verified": True,
            "reconnect_attempts": reconnect_attempts,
            "acknowledged_state_preserved": True,
            "acknowledged_events_preserved": True,
            "run_fence_preserved": True,
            "connection_bound": 2,
            "observed_application_connections": connection_count,
            "observed_acknowledged_state_loss": 0,
            "external_fencing_required": True,
            "split_brain_certification": False,
            "open_production_gates": [
                "deployment-specific automatic failover and external fencing",
                "network-partition and split-brain fault injection",
                "multi-fault and simultaneous primary/standby crash behavior",
                "synchronous-commit latency and availability SLO under production load",
                "old-primary rewind/rejoin and repeated role rotation",
            ],
        }
    finally:
        store.close()


def _arguments() -> SynchronousProbeArguments:
    parser = argparse.ArgumentParser(
        description=(
            "Create an owned PostgreSQL 17 pair, prove remote-apply false-ack "
            "prevention, kill the primary, promote, and verify acknowledged state."
        )
    )
    parser.add_argument("--allow-topology-create", action="store_true")
    parser.add_argument("--image", default=base.POSTGRES_IMAGE)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--outage-timeout", type=float, default=1.0)
    parser.add_argument("--blocked-observation", type=float, default=0.5)
    parsed = parser.parse_args()
    return SynchronousProbeArguments(
        allow_topology_create=bool(parsed.allow_topology_create),
        image=str(parsed.image),
        timeout_seconds=float(parsed.timeout),
        outage_timeout_seconds=float(parsed.outage_timeout),
        blocked_observation_seconds=float(parsed.blocked_observation),
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
            "PostgreSQL synchronous-failover cleanup failed: "
            + "; ".join(cleanup.failures)
        )
    if output is None:
        raise AssertionError("PostgreSQL synchronous failover probe returned no result")
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
