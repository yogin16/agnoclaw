#!/usr/bin/env python3
"""Prove external writer-authority containment during a real PostgreSQL split brain."""

from __future__ import annotations

import argparse
import json
import logging
import socket
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
    EtcdPostgresWriterAuthority,
    PostgresRuntimeStore,
    PostgresWriterAuthorityError,
)
from agnoclaw.runtime.lifecycle import RunSnapshot


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
etcd = cast(Any, _load_sibling("etcd_writer_authority_probe"))
if TYPE_CHECKING:
    from scripts.postgres_failover_probe import OwnedResources, TopologyNames

AUTHORITY_ID = etcd.AUTHORITY_ID
PRIMARY_SERVER_ID = "agnoclaw_failover_primary"
STANDBY_SERVER_ID = "agnoclaw_failover_standby"


@dataclass(frozen=True)
class SplitBrainProbeArguments:
    allow_topology_create: bool
    image: str
    etcd_image: str
    timeout_seconds: float
    outage_timeout_seconds: float


def _validate_arguments(args: SplitBrainProbeArguments) -> None:
    base._validate_arguments(
        base.ProbeArguments(
            allow_topology_create=args.allow_topology_create,
            image=args.image,
            timeout_seconds=args.timeout_seconds,
            outage_timeout_seconds=args.outage_timeout_seconds,
        )
    )
    etcd._validate_arguments(
        etcd.ProbeArguments(
            allow_container_create=args.allow_topology_create,
            image=args.etcd_image,
            timeout_seconds=args.timeout_seconds,
            lease_ttl_seconds=15,
        )
    )


def _writer_state(dsn: str) -> dict[str, object]:
    return base._query(
        dsn,
        """
        SELECT current_setting('cluster_name') AS server_id,
               pg_is_in_recovery() AS in_recovery,
               current_setting('transaction_read_only') AS transaction_read_only
        """,
    )


def _assert_two_writers(primary_dsn: str, standby_dsn: str) -> None:
    expected = {
        primary_dsn: PRIMARY_SERVER_ID,
        standby_dsn: STANDBY_SERVER_ID,
    }
    for dsn, server_id in expected.items():
        state = _writer_state(dsn)
        if state != {
            "server_id": server_id,
            "in_recovery": False,
            "transaction_read_only": "off",
        }:
            raise RuntimeError("fault injection did not produce two exact writable nodes")


def _count_run(dsn: str, run_id: str) -> int:
    return int(
        base._query(
            dsn,
            "SELECT count(*) AS count FROM runtime_runs WHERE run_id = %s",
            (run_id,),
        )["count"]
    )


def _snapshot(run_id: str, probe_id: str) -> RunSnapshot:
    return RunSnapshot(
        run_id=run_id,
        tenant_id="agnoclaw-probe",
        user_id="agnoclaw-probe",
        session_id=f"split-brain-{probe_id}",
    )


def _denied_create(
    store: PostgresRuntimeStore,
    snapshot: RunSnapshot,
) -> tuple[str, float]:
    started = time.monotonic()
    try:
        store.create_run(snapshot)
    except PostgresWriterAuthorityError as exc:
        reason = str((exc.details or {}).get("reason", ""))
        if not reason:
            raise RuntimeError("authority denial did not expose a safe reason code") from exc
        return reason, time.monotonic() - started
    raise RuntimeError("writer-authority guard admitted a forbidden mutation")


def _store(
    dsn: str,
    *,
    application_name: str,
    authority: EtcdPostgresWriterAuthority | None = None,
    outage_timeout_seconds: float,
) -> PostgresRuntimeStore:
    return PostgresRuntimeStore(
        dsn,
        min_pool_size=1,
        max_pool_size=1,
        max_waiting=1,
        pool_timeout_seconds=outage_timeout_seconds,
        connect_timeout_seconds=2,
        application_name=application_name,
        writer_authority=authority,
        writer_authority_check_timeout_seconds=0.25,
        writer_authority_safety_margin_seconds=0.5,
        writer_authority_max_transaction_seconds=0.5,
    )


def _start_etcd_authority(
    *,
    container: str,
    image: str,
    timeout: float,
) -> tuple[str, str]:
    created = False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = int(reservation.getsockname()[1])
    try:
        etcd._docker(
            "run",
            "--detach",
            "--name",
            container,
            "--publish",
            f"127.0.0.1:{port}:2379",
            image,
            "/usr/local/bin/etcd",
            "--name",
            "authority",
            "--listen-client-urls",
            "http://0.0.0.0:2379",
            "--advertise-client-urls",
            "http://0.0.0.0:2379",
            "--listen-peer-urls",
            "http://127.0.0.1:2380",
            "--initial-advertise-peer-urls",
            "http://127.0.0.1:2380",
            "--initial-cluster",
            "authority=http://127.0.0.1:2380",
            "--initial-cluster-state",
            "new",
            "--log-level",
            "warn",
            timeout=timeout,
        )
        created = True
        if etcd._published_port(container, timeout=5) != port:
            raise RuntimeError("etcd did not bind its exact reserved loopback port")
        endpoint = f"http://127.0.0.1:{port}"
        etcd._wait_ready(endpoint, timeout=timeout)
        status = etcd._post(endpoint, "/v3/maintenance/status", {}, timeout=2)
        header = status.get("header")
        if not isinstance(header, dict) or not isinstance(
            header.get("cluster_id"), str
        ):
            raise RuntimeError("live etcd status omitted its cluster identity")
        return endpoint, header["cluster_id"]
    except BaseException as exc:
        if created:
            try:
                etcd._docker("rm", "--force", container, timeout=min(timeout, 15))
            except BaseException as cleanup_exc:
                exc.add_note(str(cleanup_exc))
        raise


def _set_authority(
    endpoint: str,
    *,
    server_id: str,
    timeout: float,
) -> int:
    lease_id = etcd._grant_lease(endpoint, ttl=15, timeout=timeout)
    etcd._put(endpoint, lease_id=lease_id, server_id=server_id, timeout=timeout)
    return int(lease_id)


def _revoke_authority(endpoint: str, lease_id: int, *, timeout: float) -> None:
    etcd._post(
        endpoint,
        "/v3/lease/revoke",
        {"ID": str(lease_id)},
        timeout=timeout,
    )


def _probe(
    args: SplitBrainProbeArguments,
    names: TopologyNames,
    owned: OwnedResources,
) -> dict[str, object]:
    password = uuid4().hex
    image_started = time.monotonic()
    image_identity = base._resolve_image(args.image, timeout=args.timeout_seconds)
    image_resolution_seconds = time.monotonic() - image_started
    drill_started = time.monotonic()
    etcd_container = f"{etcd.RESOURCE_PREFIX}{names.probe_id}"
    endpoint, etcd_cluster_id = _start_etcd_authority(
        container=etcd_container,
        image=args.etcd_image,
        timeout=args.timeout_seconds,
    )
    stores: list[PostgresRuntimeStore] = []
    seed_id = f"pg_split_seed_{names.probe_id}"
    old_divergent_id = f"pg_split_old_{names.probe_id}"
    new_divergent_id = f"pg_split_new_{names.probe_id}"
    stale_denied_id = f"pg_split_stale_denied_{names.probe_id}"
    authoritative_id = f"pg_split_authoritative_{names.probe_id}"
    outage_primary_id = f"pg_split_outage_primary_{names.probe_id}"
    outage_standby_id = f"pg_split_outage_standby_{names.probe_id}"
    recovered_id = f"pg_split_recovered_{names.probe_id}"
    revalidation_version = 1_000_000_000 + int(names.probe_id[:7], 16)
    authorities: list[EtcdPostgresWriterAuthority] = []
    try:
        primary_dsn, standby_dsn, _, _ = base._create_topology(
            names,
            image=image_identity,
            password=password,
            timeout=args.timeout_seconds,
            owned=owned,
        )
        seed = _store(
            primary_dsn,
            application_name=f"agnoclaw-split-seed-{names.probe_id}",
            outage_timeout_seconds=args.outage_timeout_seconds,
        )
        stores.append(seed)
        seed.create_run(_snapshot(seed_id, names.probe_id))
        common_lsn = str(
            base._query(
                primary_dsn,
                "SELECT pg_current_wal_flush_lsn()::text AS lsn",
            )["lsn"]
        )
        base._wait_replayed(standby_dsn, common_lsn, timeout=args.timeout_seconds)
        seed.close()
        stores.remove(seed)

        promoted = bool(
            base._query(
                standby_dsn,
                "SELECT pg_promote(true, %s) AS promoted",
                (int(args.timeout_seconds),),
            )["promoted"]
        )
        if not promoted:
            raise RuntimeError("standby promotion failed during split-brain injection")
        _assert_two_writers(primary_dsn, standby_dsn)

        old_unguarded = _store(
            primary_dsn,
            application_name=f"agnoclaw-split-old-unguarded-{names.probe_id}",
            outage_timeout_seconds=args.outage_timeout_seconds,
        )
        new_unguarded = _store(
            standby_dsn,
            application_name=f"agnoclaw-split-new-unguarded-{names.probe_id}",
            outage_timeout_seconds=args.outage_timeout_seconds,
        )
        stores.extend((old_unguarded, new_unguarded))
        old_unguarded.create_run(_snapshot(old_divergent_id, names.probe_id))
        new_unguarded.create_run(_snapshot(new_divergent_id, names.probe_id))
        if (
            _count_run(primary_dsn, old_divergent_id) != 1
            or _count_run(standby_dsn, old_divergent_id) != 0
            or _count_run(primary_dsn, new_divergent_id) != 0
            or _count_run(standby_dsn, new_divergent_id) != 1
        ):
            raise RuntimeError("unguarded split-brain histories did not diverge exactly")
        old_unguarded.close()
        stores.remove(old_unguarded)
        new_unguarded.close()
        stores.remove(new_unguarded)

        lease_id = _set_authority(
            endpoint,
            server_id=PRIMARY_SERVER_ID,
            timeout=2,
        )
        primary_authority = EtcdPostgresWriterAuthority(
            endpoint=endpoint,
            key=etcd.KEY,
            authority_id=AUTHORITY_ID,
            cluster_id=etcd_cluster_id,
            allow_insecure_loopback=True,
            ttl_uncertainty_seconds=0.25,
        )
        standby_authority = EtcdPostgresWriterAuthority(
            endpoint=endpoint,
            key=etcd.KEY,
            authority_id=AUTHORITY_ID,
            cluster_id=etcd_cluster_id,
            allow_insecure_loopback=True,
            ttl_uncertainty_seconds=0.25,
        )
        authorities.extend((primary_authority, standby_authority))
        guarded_primary = _store(
            primary_dsn,
            application_name=f"agnoclaw-split-old-guarded-{names.probe_id}",
            authority=primary_authority,
            outage_timeout_seconds=args.outage_timeout_seconds,
        )
        stores.append(guarded_primary)

        _revoke_authority(endpoint, lease_id, timeout=2)
        lease_id = _set_authority(
            endpoint,
            server_id=STANDBY_SERVER_ID,
            timeout=2,
        )
        guarded_standby = _store(
            standby_dsn,
            application_name=f"agnoclaw-split-new-guarded-{names.probe_id}",
            authority=standby_authority,
            outage_timeout_seconds=args.outage_timeout_seconds,
        )
        stores.append(guarded_standby)

        stale_reason, stale_denial_seconds = _denied_create(
            guarded_primary,
            _snapshot(stale_denied_id, names.probe_id),
        )
        if stale_reason != "server_identity_mismatch":
            raise RuntimeError("stale writer failed for an unexpected authority reason")
        if _count_run(primary_dsn, stale_denied_id) or _count_run(standby_dsn, stale_denied_id):
            raise RuntimeError("stale-writer denial left a runtime row")

        guarded_standby.create_run(_snapshot(authoritative_id, names.probe_id))
        if _count_run(primary_dsn, authoritative_id) or _count_run(
            standby_dsn, authoritative_id
        ) != 1:
            raise RuntimeError("authoritative writer mutation reached the wrong timeline")

        timeout_started = time.monotonic()
        try:
            with guarded_standby._transaction() as conn:  # noqa: SLF001 - timeout oracle
                conn.execute("SELECT pg_sleep(1)")
        except PostgresWriterAuthorityError as exc:
            timeout_reason = str((exc.details or {}).get("reason", ""))
        else:
            raise RuntimeError("transaction outlived the admitted authority lease")
        transaction_timeout_seconds = time.monotonic() - timeout_started
        if timeout_reason != "transaction_timeout":
            raise RuntimeError("server transaction timeout returned an unexpected reason")
        if transaction_timeout_seconds > 1.5:
            raise RuntimeError("server transaction timeout exceeded its observation bound")

        _revoke_authority(endpoint, lease_id, timeout=2)
        etcd._docker("stop", "--timeout", "1", etcd_container, timeout=5)
        primary_outage_reason, primary_outage_seconds = _denied_create(
            guarded_primary,
            _snapshot(outage_primary_id, names.probe_id),
        )
        standby_outage_reason, standby_outage_seconds = _denied_create(
            guarded_standby,
            _snapshot(outage_standby_id, names.probe_id),
        )
        if not {primary_outage_reason, standby_outage_reason} <= {
            "etcd_unavailable",
            "etcd_timeout",
        }:
            raise RuntimeError("authority outage did not fail closed on both writers")

        etcd._docker("start", etcd_container, timeout=5)
        restarted_endpoint = (
            f"http://127.0.0.1:{etcd._published_port(etcd_container, timeout=5)}"
        )
        if restarted_endpoint != endpoint:
            raise RuntimeError("etcd loopback endpoint changed across restart")
        etcd._wait_ready(endpoint, timeout=min(args.timeout_seconds, 15))
        lease_id = _set_authority(
            endpoint,
            server_id=STANDBY_SERVER_ID,
            timeout=2,
        )
        try:
            with guarded_standby._transaction() as conn:  # noqa: SLF001 - commit oracle
                conn.execute(
                    """
                    INSERT INTO runtime_schema_migrations(version, applied_at)
                    VALUES (%s, CURRENT_TIMESTAMP)
                    """,
                    (revalidation_version,),
                )
                _revoke_authority(endpoint, lease_id, timeout=2)
                lease_id = _set_authority(
                    endpoint,
                    server_id=STANDBY_SERVER_ID,
                    timeout=2,
                )
        except PostgresWriterAuthorityError as exc:
            commit_revalidation_reason = str((exc.details or {}).get("reason", ""))
        else:
            raise RuntimeError("authority change immediately before commit was admitted")
        if commit_revalidation_reason != "authority_changed":
            raise RuntimeError("commit revalidation returned an unexpected reason")
        for dsn in (primary_dsn, standby_dsn):
            if int(
                base._query(
                    dsn,
                    """
                    SELECT count(*) AS count FROM runtime_schema_migrations
                    WHERE version = %s
                    """,
                    (revalidation_version,),
                )["count"]
            ):
                raise RuntimeError("commit revalidation denial failed to roll back")

        guarded_standby.create_run(_snapshot(recovered_id, names.probe_id))
        if _count_run(primary_dsn, recovered_id) or _count_run(standby_dsn, recovered_id) != 1:
            raise RuntimeError("authority recovery did not restore only the named writer")

        primary_connections = int(
            base._query(
                primary_dsn,
                """
                SELECT count(*) AS count FROM pg_stat_activity
                WHERE application_name = %s
                """,
                (f"agnoclaw-split-old-guarded-{names.probe_id}",),
            )["count"]
        )
        standby_connections = int(
            base._query(
                standby_dsn,
                """
                SELECT count(*) AS count FROM pg_stat_activity
                WHERE application_name = %s
                """,
                (f"agnoclaw-split-new-guarded-{names.probe_id}",),
            )["count"]
        )
        if primary_connections > 1 or standby_connections > 1:
            raise RuntimeError("split-brain probe exceeded its per-store connection cap")

        return {
            "status": "passed",
            "scope": "owned_two_node_real_dual_writer_application_authority_probe",
            "production_certification": False,
            "external_authority_is_test_double": False,
            "real_etcd_authority_adapter": True,
            "etcd_image_digest": args.etcd_image,
            "etcd_single_node_test_topology": True,
            "postgres_image": args.image,
            "postgres_image_digest": image_identity,
            "postgres_server_version": str(
                base._query(primary_dsn, "SHOW server_version")["server_version"]
            ),
            "real_two_writer_fault_injected": True,
            "network_partition_certification": False,
            "unsafe_promotion_with_old_writer_live": True,
            "unguarded_writers_both_committed": True,
            "unguarded_histories_diverged": True,
            "authority_fence_source": "etcd_mod_revision",
            "stale_writer_denied": True,
            "stale_writer_denial_reason": stale_reason,
            "stale_writer_denial_seconds": round(stale_denial_seconds, 3),
            "authoritative_writer_committed": True,
            "commit_revalidation_denied": True,
            "commit_revalidation_reason": commit_revalidation_reason,
            "commit_revalidation_rolled_back": True,
            "authority_outage_denied_both_writers": True,
            "authority_process_stopped_and_restarted": True,
            "primary_authority_outage_seconds": round(primary_outage_seconds, 3),
            "standby_authority_outage_seconds": round(standby_outage_seconds, 3),
            "transaction_timeout_reason": timeout_reason,
            "transaction_timeout_seconds": round(transaction_timeout_seconds, 3),
            "transaction_timeout_bound_seconds": 0.5,
            "transaction_timeout_observation_bound_seconds": 1.5,
            "authority_recovery_committed_on_named_writer": True,
            "observed_primary_application_connections": primary_connections,
            "observed_standby_application_connections": standby_connections,
            "per_store_connection_bound": 1,
            "arbitrary_client_fencing": False,
            "physical_fencing_certification": False,
            "watchdog_or_stonith_required": True,
            "open_production_gates": [
                "watchdog or STONITH proof for arbitrary clients and paused hosts",
                "production TLS/RBAC, multi-member etcd quorum, and controller election",
                "multiple simultaneous faults and production RPO/RTO SLOs",
            ],
            "image_resolution_seconds": round(image_resolution_seconds, 3),
            "topology_and_drill_seconds": round(time.monotonic() - drill_started, 3),
        }
    finally:
        for store in reversed(stores):
            store.close()
        for authority in reversed(authorities):
            authority.close()
        try:
            etcd._docker(
                "rm",
                "--force",
                etcd_container,
                timeout=min(args.timeout_seconds, 15),
            )
        except BaseException as cleanup_exc:
            active = sys.exception()
            if active is not None:
                active.add_note(str(cleanup_exc))
            else:
                raise


def _arguments() -> SplitBrainProbeArguments:
    parser = argparse.ArgumentParser(
        description=(
            "Create an owned PostgreSQL 17 split brain and prove application-level "
            "external writer-authority containment."
        )
    )
    parser.add_argument("--allow-topology-create", action="store_true")
    parser.add_argument("--image", default=base.POSTGRES_IMAGE)
    parser.add_argument("--etcd-image", default=etcd.ETCD_IMAGE)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--outage-timeout", type=float, default=1.0)
    parsed = parser.parse_args()
    return SplitBrainProbeArguments(
        allow_topology_create=bool(parsed.allow_topology_create),
        image=str(parsed.image),
        etcd_image=str(parsed.etcd_image),
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
            "PostgreSQL split-brain probe cleanup failed: " + "; ".join(cleanup.failures)
        )
    if output is None:
        raise AssertionError("PostgreSQL split-brain probe returned no result")
    output["owned_resources_created"] = (
        len(owned.containers) + len(owned.volumes) + len(owned.networks) + 1
    )
    output["owned_resources_removed"] = len(cleanup.resources_removed) + 1
    output["cleanup_complete"] = True
    output["elapsed_seconds"] = round(time.monotonic() - started, 3)
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
