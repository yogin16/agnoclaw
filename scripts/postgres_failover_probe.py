#!/usr/bin/env python3
"""Certify lag, catch-up, fenced promotion, and pool failover on an owned PG pair."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import logging
import math
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from agnoclaw import (
    CandidateAction,
    CandidateAuthor,
    CandidateEvaluation,
    CandidateRisk,
    CandidateState,
    EvaluationVerdict,
    LearningCandidate,
    LearningLedgerConnectionLostError,
    LearningLedgerOverloadedError,
    LearningOwner,
    LearningTarget,
    LocalArtifactStore,
    PostgresLearningLedger,
    PromotionActor,
)
from agnoclaw.runtime import (
    ArtifactScope,
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

POSTGRES_IMAGE = "postgres:17-alpine"
DATABASE = "agnoclaw_failover_test"
RESOURCE_PREFIX = "agnoclaw-pg-failover-"
_POSTGRES_DIGEST_RE = re.compile(r"^postgres@sha256:[0-9a-f]{64}$")
_RESOURCE_RE = re.compile(
    r"^agnoclaw-pg-failover-[0-9a-f]{16}-(?:primary|standby|backup|rewind-[ab]|net|data-[ab])$"
)


@dataclass(frozen=True)
class ProbeArguments:
    allow_topology_create: bool
    image: str
    timeout_seconds: float
    outage_timeout_seconds: float


@dataclass(frozen=True)
class TopologyNames:
    probe_id: str
    primary: str
    standby: str
    backup: str
    network: str
    primary_volume: str
    standby_volume: str


@dataclass
class OwnedResources:
    """Exact UUID-named resources successfully created by this process."""

    containers: list[str] = field(default_factory=list)
    volumes: list[str] = field(default_factory=list)
    networks: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CleanupResult:
    resources_removed: tuple[str, ...]
    failures: tuple[str, ...]


def _validate_arguments(args: ProbeArguments) -> None:
    if not args.allow_topology_create:
        raise ValueError(
            "--allow-topology-create is required because the probe creates and removes "
            "two disposable PostgreSQL data volumes"
        )
    if args.image != POSTGRES_IMAGE and not _POSTGRES_DIGEST_RE.fullmatch(args.image):
        raise ValueError(
            f"--image must be exactly {POSTGRES_IMAGE!r} or an immutable "
            "postgres@sha256:<64-lowercase-hex> digest"
        )
    if not 10 <= args.timeout_seconds <= 300:
        raise ValueError("--timeout must be between 10 and 300 seconds")
    if not 0 < args.outage_timeout_seconds <= min(10, args.timeout_seconds):
        raise ValueError(
            "--outage-timeout must be positive and no greater than both 10 seconds "
            "and --timeout"
        )


def _resource_names(probe_id: str) -> TopologyNames:
    if not re.fullmatch(r"[0-9a-f]{16}", probe_id):
        raise ValueError("probe_id must be exactly 16 lowercase hexadecimal characters")
    base = f"{RESOURCE_PREFIX}{probe_id}"
    names = TopologyNames(
        probe_id=probe_id,
        primary=f"{base}-primary",
        standby=f"{base}-standby",
        backup=f"{base}-backup",
        network=f"{base}-net",
        primary_volume=f"{base}-data-a",
        standby_volume=f"{base}-data-b",
    )
    generated = (value for value in names.__dict__.values() if value != probe_id)
    if not all(_RESOURCE_RE.fullmatch(value) for value in generated):
        raise AssertionError("generated resource name escaped the owned-resource grammar")
    return names


def _docker(*arguments: str, timeout: float) -> subprocess.CompletedProcess[str]:
    failed_action: str | None = None
    try:
        result = subprocess.run(
            ["docker", *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        failed_action = arguments[0] if arguments else "docker"
    if failed_action is not None:
        # Docker argv can contain the generated test-only database password. Keep
        # normalized failures content-free even in verbose traceback renderers.
        raise RuntimeError(f"Docker action {failed_action!r} failed")
    return result


def _create_resource(
    owned: OwnedResources,
    kind: str,
    name: str,
    *arguments: str,
    timeout: float,
) -> None:
    if not _RESOURCE_RE.fullmatch(name):
        raise ValueError("refusing to create a resource outside the probe namespace")
    _docker(*arguments, timeout=timeout)
    getattr(owned, kind).append(name)


def _container_running(container: str, *, timeout: float) -> bool:
    if not _RESOURCE_RE.fullmatch(container):
        raise ValueError("refusing to inspect a container outside the probe namespace")
    result = _docker(
        "inspect",
        "--format",
        "{{.State.Running}}",
        container,
        timeout=timeout,
    )
    return result.stdout.strip() == "true"


def _published_loopback_port(container: str, *, timeout: float) -> int:
    result = _docker("port", container, "5432/tcp", timeout=timeout)
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) != 1 or not lines[0].startswith("127.0.0.1:"):
        raise RuntimeError("PostgreSQL must publish exactly one IPv4 loopback port")
    try:
        port = int(lines[0].rsplit(":", 1)[1])
    except ValueError as exc:
        raise RuntimeError("PostgreSQL published an invalid loopback port") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("PostgreSQL published an out-of-range loopback port")
    return port


def _network_subnet(network: str, *, timeout: float) -> str:
    if not _RESOURCE_RE.fullmatch(network):
        raise ValueError("refusing to inspect a network outside the probe namespace")
    result = _docker(
        "network",
        "inspect",
        "--format",
        "{{(index .IPAM.Config 0).Subnet}}",
        network,
        timeout=timeout,
    )
    try:
        subnet = ipaddress.ip_network(result.stdout.strip(), strict=True)
    except ValueError as exc:
        raise RuntimeError("Docker network returned an invalid subnet") from exc
    if not subnet.is_private or subnet.version != 4:
        raise RuntimeError("probe network must have one private IPv4 subnet")
    return str(subnet)


def _image_identity(image: str, *, timeout: float) -> str:
    result = _docker(
        "image",
        "inspect",
        "--format",
        "{{index .RepoDigests 0}}",
        image,
        timeout=timeout,
    )
    identity = result.stdout.strip()
    if not _POSTGRES_DIGEST_RE.fullmatch(identity):
        raise RuntimeError("PostgreSQL image did not resolve to one content digest")
    return identity


def _pull_image_with_retry(image: str, *, deadline: float) -> None:
    for attempt in range(3):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            _docker("image", "pull", image, timeout=remaining)
            return
        except RuntimeError:
            if attempt == 2:
                raise
            delay = min(0.5 * (2**attempt), max(0.0, deadline - time.monotonic()))
            if delay <= 0:
                raise
            time.sleep(delay)
    raise RuntimeError("Docker image resolution exceeded its bounded timeout")


def _resolve_image(image: str, *, timeout: float) -> str:
    """Resolve the allowed tag or pinned digest to one immutable content identity."""
    deadline = time.monotonic() + timeout
    if _POSTGRES_DIGEST_RE.fullmatch(image):
        try:
            identity = _image_identity(image, timeout=timeout)
        except RuntimeError:
            # A pinned digest is content-addressed, so pulling it is exactly as
            # trustworthy as a cached copy — and fresh CI runners have no cache.
            _pull_image_with_retry(image, deadline=deadline)
            identity = _image_identity(image, timeout=max(0.1, deadline - time.monotonic()))
        if identity != image:
            raise RuntimeError("cached PostgreSQL image did not match the requested digest")
        return identity
    _pull_image_with_retry(image, deadline=deadline)
    return _image_identity(image, timeout=max(0.1, deadline - time.monotonic()))


def _single_dsn(*, port: int, password: str, application_name: str) -> str:
    return (
        f"postgresql://postgres:{password}@127.0.0.1:{port}/{DATABASE}"
        f"?application_name={application_name}"
    )


def _multi_host_dsn(
    *, primary_port: int, standby_port: int, password: str, application_name: str
) -> str:
    return (
        "host=127.0.0.1,127.0.0.1 "
        f"port={primary_port},{standby_port} dbname={DATABASE} user=postgres "
        f"password={password} target_session_attrs=read-write connect_timeout=1 "
        f"application_name={application_name}"
    )


def _connect(dsn: str, *, timeout: float):  # type: ignore[no-untyped-def]
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(
        dsn,
        autocommit=True,
        connect_timeout=max(1, math.ceil(timeout)),
        row_factory=dict_row,
    )


def _query(dsn: str, sql: str, parameters: tuple[object, ...] = ()) -> dict[str, Any]:
    with _connect(dsn, timeout=2) as conn:
        row = conn.execute(sql, parameters).fetchone()
    if row is None:
        raise RuntimeError("PostgreSQL probe query returned no row")
    return dict(row)


def _wait_for(
    description: str,
    predicate,  # type: ignore[no-untyped-def]
    *,
    timeout: float,
) -> object:
    deadline = time.monotonic() + timeout
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except Exception as exc:  # noqa: BLE001 - expected while a node changes state
            last_error = exc
        time.sleep(0.2)
    raise RuntimeError(f"timed out waiting for {description}") from last_error


def _lsn_to_int(lsn: str) -> int:
    try:
        high, low = lsn.split("/", 1)
        return (int(high, 16) << 32) + int(low, 16)
    except (ValueError, AttributeError) as exc:
        raise ValueError("invalid PostgreSQL LSN") from exc


def _wait_replayed(standby_dsn: str, target_lsn: str, *, timeout: float) -> str:
    def replayed() -> str | None:
        row = _query(
            standby_dsn,
            """
            SELECT pg_last_wal_replay_lsn()::text AS replay_lsn,
                   pg_last_wal_replay_lsn() >= %s::pg_lsn AS caught_up
            """,
            (target_lsn,),
        )
        return str(row["replay_lsn"]) if row["caught_up"] else None

    return str(_wait_for("standby WAL replay catch-up", replayed, timeout=timeout))


def _assert_standby_read_only(standby_dsn: str) -> str:
    import psycopg

    row = _query(
        standby_dsn,
        """
        SELECT pg_is_in_recovery() AS in_recovery,
               current_setting('transaction_read_only') AS transaction_read_only
        """,
    )
    if row != {"in_recovery": True, "transaction_read_only": "on"}:
        raise RuntimeError("standby did not advertise strict read-only recovery")
    try:
        with _connect(standby_dsn, timeout=2) as conn:
            conn.execute("CREATE TEMP TABLE agnoclaw_failover_write_probe(id integer)")
    except psycopg.errors.ReadOnlySqlTransaction as exc:
        return str(exc.sqlstate)
    raise RuntimeError("hot standby unexpectedly accepted a write")


def _require_safe_promotion(
    *, primary_running: bool, replay_lsn: str, acknowledged_lsn: str
) -> None:
    if primary_running:
        raise RuntimeError("refusing promotion until the old primary is fenced")
    if _lsn_to_int(replay_lsn) < _lsn_to_int(acknowledged_lsn):
        raise RuntimeError("refusing promotion before acknowledged WAL is replayed")


def _cleanup_topology(
    owned: OwnedResources,
    *,
    timeout: float,
) -> CleanupResult:
    """Remove only resources this process recorded as successfully created."""
    removed: list[str] = []
    failures: list[str] = []
    for container in reversed(owned.containers):
        try:
            _docker("rm", "--force", container, timeout=timeout)
            removed.append(container)
        except Exception as exc:  # noqa: BLE001 - every exact resource gets an attempt
            failures.append(f"container cleanup failed ({container}; {type(exc).__name__})")
    for volume in reversed(owned.volumes):
        try:
            _docker("volume", "rm", volume, timeout=timeout)
            removed.append(volume)
        except Exception as exc:  # noqa: BLE001 - every exact resource gets an attempt
            failures.append(f"volume cleanup failed ({volume}; {type(exc).__name__})")
    for network in reversed(owned.networks):
        try:
            _docker("network", "rm", network, timeout=timeout)
            removed.append(network)
        except Exception as exc:  # noqa: BLE001 - every exact resource gets an attempt
            failures.append(f"network cleanup failed ({network}; {type(exc).__name__})")
    return CleanupResult(tuple(removed), tuple(failures))


def _create_topology(
    names: TopologyNames,
    *,
    image: str,
    password: str,
    timeout: float,
    owned: OwnedResources,
) -> tuple[str, str, int, int]:
    label = f"io.agnoclaw.postgres-failover-probe={names.probe_id}"
    _create_resource(
        owned,
        "networks",
        names.network,
        "network",
        "create",
        "--label",
        label,
        names.network,
        timeout=timeout,
    )
    network_subnet = _network_subnet(names.network, timeout=timeout)
    for volume in (names.primary_volume, names.standby_volume):
        _create_resource(
            owned,
            "volumes",
            volume,
            "volume",
            "create",
            "--label",
            label,
            volume,
            timeout=timeout,
        )
    _create_resource(
        owned,
        "containers",
        names.primary,
        "create",
        "--name",
        names.primary,
        "--label",
        label,
        "--network",
        names.network,
        "--network-alias",
        "primary",
        "--publish",
        "127.0.0.1::5432",
        "--env",
        f"POSTGRES_PASSWORD={password}",
        "--env",
        f"PGPASSWORD={password}",
        "--env",
        f"POSTGRES_DB={DATABASE}",
        "--env",
        "POSTGRES_INITDB_ARGS=--data-checksums",
        "--volume",
        f"{names.primary_volume}:/var/lib/postgresql/data",
        image,
        "postgres",
        "-c",
        "cluster_name=agnoclaw_failover_primary",
        "-c",
        "wal_level=replica",
        "-c",
        "max_wal_senders=10",
        "-c",
        "wal_keep_size=64MB",
        timeout=timeout,
    )
    _docker("start", names.primary, timeout=timeout)
    primary_port = _published_loopback_port(names.primary, timeout=timeout)
    primary_dsn = _single_dsn(
        port=primary_port,
        password=password,
        application_name=f"agnoclaw-failover-setup-{names.probe_id}",
    )
    _wait_for(
        "primary readiness",
        lambda: not bool(
            _query(primary_dsn, "SELECT pg_is_in_recovery() AS value")["value"]
        ),
        timeout=timeout,
    )
    _docker(
        "exec",
        "--user",
        "postgres",
        names.primary,
        "sh",
        "-ceu",
        f"printf '%s\\n' 'host replication postgres {network_subnet} scram-sha-256' "
        '>> "$PGDATA/pg_hba.conf"',
        timeout=timeout,
    )
    _docker(
        "exec",
        "--user",
        "postgres",
        names.primary,
        "psql",
        "--username",
        "postgres",
        "--dbname",
        DATABASE,
        "--set",
        "ON_ERROR_STOP=1",
        "--command",
        "SELECT pg_reload_conf()",
        timeout=timeout,
    )
    _create_resource(
        owned,
        "containers",
        names.backup,
        "create",
        "--name",
        names.backup,
        "--label",
        label,
        "--network",
        names.network,
        "--env",
        f"PGPASSWORD={password}",
        "--env",
        (
            "PRIMARY_CONNINFO=host=primary port=5432 user=postgres "
            f"application_name=standby_{names.probe_id}"
        ),
        "--volume",
        f"{names.standby_volume}:/var/lib/postgresql/data",
        image,
        "sh",
        "-ceu",
        """
        chown -R postgres:postgres "$PGDATA"
        exec gosu postgres pg_basebackup \
          --dbname "$PRIMARY_CONNINFO" \
          --pgdata "$PGDATA" \
          --format=plain \
          --wal-method=stream \
          --write-recovery-conf \
          --checkpoint=fast \
          --no-password
        """,
        timeout=timeout,
    )
    # The disposable process supplies its test-only password through the environment;
    # pg_basebackup persists only non-secret host/user/application settings in PGDATA.
    _docker("start", "--attach", names.backup, timeout=timeout)
    _create_resource(
        owned,
        "containers",
        names.standby,
        "create",
        "--name",
        names.standby,
        "--label",
        label,
        "--network",
        names.network,
        "--network-alias",
        "standby",
        "--publish",
        "127.0.0.1::5432",
        "--env",
        f"PGPASSWORD={password}",
        "--volume",
        f"{names.standby_volume}:/var/lib/postgresql/data",
        image,
        "postgres",
        "-c",
        "cluster_name=agnoclaw_failover_standby",
        "-c",
        "hot_standby=on",
        timeout=timeout,
    )
    _docker("start", names.standby, timeout=timeout)
    standby_port = _published_loopback_port(names.standby, timeout=timeout)
    standby_dsn = _single_dsn(
        port=standby_port,
        password=password,
        application_name=f"agnoclaw-failover-observer-{names.probe_id}",
    )
    _wait_for(
        "hot standby readiness",
        lambda: bool(_query(standby_dsn, "SELECT pg_is_in_recovery() AS value")["value"]),
        timeout=timeout,
    )
    return primary_dsn, standby_dsn, primary_port, standby_port


def _probe(args: ProbeArguments, names: TopologyNames, owned: OwnedResources) -> dict[str, object]:
    password = uuid4().hex
    image_identity = _resolve_image(args.image, timeout=args.timeout_seconds)
    primary_dsn, standby_dsn, primary_port, standby_port = _create_topology(
        names,
        image=image_identity,
        password=password,
        timeout=args.timeout_seconds,
        owned=owned,
    )
    standby_write_sqlstate = _assert_standby_read_only(standby_dsn)
    server_version = str(
        _query(primary_dsn, "SHOW server_version")["server_version"]
    )
    if server_version.split(".", 1)[0] != "17":
        raise RuntimeError("probe requires PostgreSQL server major version 17")
    application_name = f"agnoclaw-failover-store-{names.probe_id}"
    multi_dsn = _multi_host_dsn(
        primary_port=primary_port,
        standby_port=standby_port,
        password=password,
        application_name=application_name,
    )
    owner = RunOwner("agnoclaw-probe", "agnoclaw-probe")
    run_id = f"pg_failover_probe_{names.probe_id}"
    learning_owner = LearningOwner("agnoclaw-probe", "failover-probe")
    candidate_id = f"learning_failover_probe_{names.probe_id}"
    learning_application_name = f"agnoclaw-failover-learning-{names.probe_id}"
    store = PostgresRuntimeStore(
        multi_dsn,
        min_pool_size=1,
        max_pool_size=2,
        max_waiting=2,
        pool_timeout_seconds=args.outage_timeout_seconds,
        connect_timeout_seconds=2,
        application_name=application_name,
    )
    learning: PostgresLearningLedger | None = None
    artifact_directory: tempfile.TemporaryDirectory[str] | None = None
    try:
        learning = PostgresLearningLedger(
            multi_dsn,
            min_pool_size=1,
            max_pool_size=2,
            max_waiting=2,
            pool_timeout_seconds=args.outage_timeout_seconds,
            connect_timeout_seconds=2,
            application_name=learning_application_name,
        )
        with store._connection() as conn:  # noqa: SLF001 - topology routing oracle
            source_node = str(
                conn.execute("SELECT current_setting('cluster_name') AS node").fetchone()[
                    "node"
                ]
            )
        if source_node != "agnoclaw_failover_primary":
            raise RuntimeError("multi-host pool did not initially select the primary")
        with learning._connection() as conn:  # noqa: SLF001 - topology routing oracle
            learning_source_node = str(
                conn.execute("SELECT current_setting('cluster_name') AS node").fetchone()[
                    "node"
                ]
            )
        if learning_source_node != "agnoclaw_failover_primary":
            raise RuntimeError("learning pool did not initially select the primary")

        schema_lsn = str(
            _query(primary_dsn, "SELECT pg_current_wal_flush_lsn()::text AS lsn")["lsn"]
        )
        _wait_replayed(standby_dsn, schema_lsn, timeout=args.timeout_seconds)
        _query(standby_dsn, "SELECT pg_wal_replay_pause() AS paused")
        _wait_for(
            "standby replay pause",
            lambda: _query(
                standby_dsn,
                "SELECT pg_get_wal_replay_pause_state() AS state",
            )["state"]
            == "paused",
            timeout=args.timeout_seconds,
        )

        store.create_run(
            RunSnapshot(
                run_id=run_id,
                tenant_id=owner.tenant_id,
                user_id=owner.user_id,
                session_id=f"failover-probe-{names.probe_id}",
            )
        )
        claim = store.acquire_run_lease(
            run_id,
            worker_id="probe-before-promotion",
            claim_id=f"probe:{names.probe_id}",
            lease_seconds=max(120, math.ceil(args.timeout_seconds * 2)),
            owner=owner,
        ).claim
        artifact_directory = tempfile.TemporaryDirectory(
            prefix="agnoclaw-learning-failover-artifacts-"
        )
        artifact_store = LocalArtifactStore(Path(artifact_directory.name))
        content_artifact = asyncio.run(
            artifact_store.stage_json(
                {
                    "title": "Failover-safe retry rule",
                    "learning": "Use bounded idempotent retries after verified absence.",
                },
                scope=ArtifactScope(
                    run_id=run_id,
                    tenant_id=learning_owner.tenant_id,
                    user_id=owner.user_id,
                ),
                purpose="learning.candidate.content",
            )
        )
        candidate = LearningCandidate(
            candidate_id=candidate_id,
            target=LearningTarget.LEARNED_KNOWLEDGE,
            tenant_id=learning_owner.tenant_id,
            storage_namespace=learning_owner.storage_namespace,
            content_artifact=content_artifact,
            source_run_ids=(run_id,),
            evidence_artifact_ids=("failover-source-evidence",),
            confidence=0.95,
            risk=CandidateRisk.LOW,
            created_by=CandidateAuthor.OPERATOR,
            mechanism_version="postgres-failover-probe:v1",
            source_user_id=owner.user_id,
        )
        learning.create_candidate(candidate)
        qualified = learning.record_evaluation(
            CandidateEvaluation(
                evaluation_id=f"evaluation_{names.probe_id}",
                candidate_id=candidate_id,
                verdict=EvaluationVerdict.QUALIFIED,
                evaluator_digest="sha256:" + ("a" * 64),
                evidence_artifact_ids=("failover-evaluation-evidence",),
                safety_passed=True,
                evaluated_by=PromotionActor.OPERATOR,
                metrics={"held_out": 1.0},
                control_metrics={"held_out": 0.0},
            ),
            owner=learning_owner,
            expected_revision=0,
            mutation_id=f"evaluate_{names.probe_id}",
        )
        if qualified.state is not CandidateState.QUALIFIED or qualified.revision != 1:
            raise RuntimeError("learning candidate did not reach the acknowledged state")
        acknowledged_lsn = str(
            _query(primary_dsn, "SELECT pg_current_wal_flush_lsn()::text AS lsn")["lsn"]
        )
        paused_row = _query(
            standby_dsn,
            """
            SELECT pg_last_wal_replay_lsn()::text AS replay_lsn,
                   pg_wal_lsn_diff(%s::pg_lsn, pg_last_wal_replay_lsn())::bigint AS lag_bytes,
                   (SELECT count(*) FROM runtime_runs WHERE run_id = %s) AS marker_rows,
                   (SELECT count(*) FROM learning_candidates
                    WHERE candidate_id = %s) AS learning_rows
            """,
            (acknowledged_lsn, run_id, candidate_id),
        )
        measured_lag_bytes = int(paused_row["lag_bytes"])
        if (
            measured_lag_bytes <= 0
            or int(paused_row["marker_rows"]) != 0
            or int(paused_row["learning_rows"]) != 0
        ):
            raise RuntimeError("paused standby did not expose measurable pre-replay lag")

        _query(standby_dsn, "SELECT pg_wal_replay_resume() AS resumed")
        replay_lsn = _wait_replayed(
            standby_dsn,
            acknowledged_lsn,
            timeout=args.timeout_seconds,
        )
        replicated_row = _query(
            standby_dsn,
            """
            SELECT run.state, run.revision, count(lease.lease_key) AS lease_rows,
                   min(lease.fence_token) AS min_fence,
                   max(lease.fence_token) AS max_fence
            FROM runtime_runs AS run
            JOIN runtime_execution_leases AS lease ON lease.run_id = run.run_id
            WHERE run.run_id = %s
            GROUP BY run.state, run.revision
            """,
            (run_id,),
        )
        if replicated_row != {
            "state": "created",
            "revision": 0,
            "lease_rows": 2,
            "min_fence": claim.run.fence_token,
            "max_fence": claim.run.fence_token,
        }:
            raise RuntimeError("standby did not replay exact acknowledged run and fence state")
        replicated_learning = _query(
            standby_dsn,
            """
            SELECT candidate.state, candidate.revision,
                   (SELECT count(*) FROM learning_evaluations
                    WHERE candidate_id = candidate.candidate_id) AS evaluations,
                   (SELECT count(*) FROM learning_events
                    WHERE candidate_id = candidate.candidate_id) AS events
            FROM learning_candidates AS candidate
            WHERE candidate.candidate_id = %s
            """,
            (candidate_id,),
        )
        if replicated_learning != {
            "state": CandidateState.QUALIFIED.value,
            "revision": 1,
            "evaluations": 1,
            "events": 2,
        }:
            raise RuntimeError("standby did not replay exact acknowledged learning state")

        cutover_lsn = str(
            _query(primary_dsn, "SELECT pg_current_wal_flush_lsn()::text AS lsn")["lsn"]
        )
        replay_lsn = _wait_replayed(standby_dsn, cutover_lsn, timeout=args.timeout_seconds)
        fence_started = time.monotonic()
        _docker("stop", "--time", "1", names.primary, timeout=args.timeout_seconds)
        fenced_at = time.monotonic()
        primary_running = _container_running(names.primary, timeout=args.timeout_seconds)
        _require_safe_promotion(
            primary_running=primary_running,
            replay_lsn=replay_lsn,
            acknowledged_lsn=cutover_lsn,
        )
        no_writer_started = time.monotonic()
        try:
            store.get_run(run_id, owner=owner)
        except (RuntimeStoreConnectionLostError, RuntimeStoreOverloadedError) as exc:
            no_writer_error = exc
        else:
            raise RuntimeError("multi-host pool found a writable node before promotion")
        no_writer_failure_seconds = time.monotonic() - no_writer_started
        if no_writer_failure_seconds > args.outage_timeout_seconds + 2:
            raise RuntimeError("no-writer interval did not fail within the bounded window")
        learning_no_writer_started = time.monotonic()
        try:
            learning.get_candidate(candidate_id, owner=learning_owner)
        except (LearningLedgerConnectionLostError, LearningLedgerOverloadedError) as exc:
            learning_no_writer_error = exc
        else:
            raise RuntimeError("learning pool found a writable node before promotion")
        learning_no_writer_failure_seconds = time.monotonic() - learning_no_writer_started
        if learning_no_writer_failure_seconds > args.outage_timeout_seconds + 2:
            raise RuntimeError("learning no-writer interval exceeded the bounded window")
        promotion_started = time.monotonic()
        promoted = bool(
            _query(
                standby_dsn,
                "SELECT pg_promote(true, %s) AS promoted",
                (math.ceil(args.timeout_seconds),),
            )["promoted"]
        )
        if not promoted:
            raise RuntimeError("PostgreSQL did not complete promotion before timeout")
        _wait_for(
            "standby promotion",
            lambda: not bool(
                _query(standby_dsn, "SELECT pg_is_in_recovery() AS value")["value"]
            ),
            timeout=args.timeout_seconds,
        )
        promotion_seconds = time.monotonic() - promotion_started
        if fence_started > fenced_at or fenced_at > promotion_started:
            raise RuntimeError("old-primary fencing did not precede promotion")
        if _container_running(names.primary, timeout=args.timeout_seconds):
            raise RuntimeError("old primary lost its fence after promotion")

        reconnect_attempts = 0
        reconnect_errors: list[str] = []
        deadline = time.monotonic() + args.timeout_seconds
        last_error: BaseException | None = None
        while time.monotonic() < deadline:
            reconnect_attempts += 1
            try:
                snapshot = store.get_run(run_id, owner=owner)
                break
            except (RuntimeStoreConnectionLostError, RuntimeStoreOverloadedError) as exc:
                reconnect_errors.append(exc.code)
                last_error = exc
                time.sleep(0.2)
        else:
            raise RuntimeError(
                "existing multi-host pool did not reach promoted writer"
            ) from last_error
        if snapshot.state is not RunState.CREATED or snapshot.revision != 0:
            raise RuntimeError("acknowledged pre-promotion state changed")
        learning_reconnect_attempts = 0
        learning_reconnect_errors: list[str] = []
        deadline = time.monotonic() + args.timeout_seconds
        learning_last_error: BaseException | None = None
        while time.monotonic() < deadline:
            learning_reconnect_attempts += 1
            try:
                learning_record = learning.get_candidate(candidate_id, owner=learning_owner)
                break
            except (LearningLedgerConnectionLostError, LearningLedgerOverloadedError) as exc:
                learning_reconnect_errors.append(exc.code)
                learning_last_error = exc
                time.sleep(0.2)
        else:
            raise RuntimeError(
                "existing learning pool did not reach promoted writer"
            ) from learning_last_error
        if (
            learning_record.state is not CandidateState.QUALIFIED
            or learning_record.revision != qualified.revision
            or learning_record.candidate.digest != qualified.candidate.digest
            or learning_record.latest_evaluation_id != qualified.latest_evaluation_id
        ):
            raise RuntimeError("acknowledged learning state changed across promotion")
        with store._connection() as conn:  # noqa: SLF001 - topology routing oracle
            promoted_node = str(
                conn.execute("SELECT current_setting('cluster_name') AS node").fetchone()[
                    "node"
                ]
            )
        if promoted_node != "agnoclaw_failover_standby":
            raise RuntimeError("pool did not select the promoted writable node")

        renewed = store.renew_run_lease(claim, lease_seconds=60)
        if renewed.run.fence_token != claim.run.fence_token:
            raise RuntimeError("lease fence changed across safe promotion")
        queued = store.apply_transition(
            LifecycleTransition(
                run_id=run_id,
                kind=TransitionKind.QUEUE,
                transition_id=f"{run_id}:queue-after-promotion",
            ),
            expected_revision=snapshot.revision,
        ).lifecycle.after
        store.release_run_lease(renewed)
        quarantined = learning.transition_candidate(
            candidate_id,
            owner=learning_owner,
            expected_revision=learning_record.revision,
            mutation_id=f"quarantine_{names.probe_id}",
            action=CandidateAction.QUARANTINE,
        )
        if quarantined.state is not CandidateState.QUARANTINED or quarantined.revision != 2:
            raise RuntimeError("post-promotion learning mutation did not commit")
        learning_events = learning.list_events(candidate_id, owner=learning_owner)
        if [event.sequence for event in learning_events] != [1, 2, 3]:
            raise RuntimeError("learning event history changed across promotion")
        events = store.list_events(run_id, owner=owner)
        if queued.state is not RunState.QUEUED:
            raise RuntimeError("post-promotion transition did not commit")
        if [event.sequence for event in events] != list(range(1, len(events) + 1)):
            raise RuntimeError("event sequence is not contiguous after promotion")
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
            raise RuntimeError("probe exceeded its declared connection bound")
        learning_connection_count = int(
            _query(
                standby_dsn,
                """
                SELECT count(*) AS count FROM pg_stat_activity
                WHERE application_name = %s
                """,
                (learning_application_name,),
            )["count"]
        )
        if learning_connection_count > 2:
            raise RuntimeError("learning probe exceeded its declared connection bound")

        with PostgresRuntimeStore(
            multi_dsn,
            min_pool_size=1,
            max_pool_size=1,
            application_name=f"agnoclaw-failover-fresh-{names.probe_id}",
        ) as reopened:
            durable = reopened.get_run(run_id, owner=owner)
            if durable.state is not RunState.QUEUED:
                raise RuntimeError("fresh pool cannot observe post-promotion state")
        with PostgresLearningLedger(
            multi_dsn,
            min_pool_size=1,
            max_pool_size=1,
            application_name=f"agnoclaw-learning-fresh-{names.probe_id}",
        ) as reopened_learning:
            durable_learning = reopened_learning.get_candidate(
                candidate_id,
                owner=learning_owner,
            )
            if durable_learning != quarantined:
                raise RuntimeError("fresh learning pool cannot observe post-promotion state")

        return {
            "status": "passed",
            "scope": "owned_two_node_asynchronous_fenced_promotion_probe",
            "production_certification": False,
            "postgres_image": args.image,
            "postgres_image_digest": image_identity,
            "postgres_server_version": server_version,
            "streaming_replication": True,
            "standby_read_only": True,
            "standby_write_sqlstate": standby_write_sqlstate,
            "measured_replay_lag_bytes": measured_lag_bytes,
            "acknowledged_lsn_replayed_before_fence": True,
            "old_primary_fenced_before_promotion": True,
            "old_primary_fence_preserved_after_promotion": True,
            "old_primary_restarted": False,
            "promotion_seconds": round(promotion_seconds, 3),
            "no_writer_error": no_writer_error.code,
            "no_writer_error_retryable": no_writer_error.retryable,
            "no_writer_failure_seconds": round(no_writer_failure_seconds, 3),
            "no_writer_timeout_seconds": args.outage_timeout_seconds,
            "target_session_attrs": "read-write",
            "existing_pool_reconnected": True,
            "fresh_pool_verified": True,
            "reconnect_attempts": reconnect_attempts,
            "reconnect_error_codes": reconnect_errors,
            "acknowledged_state_preserved": True,
            "event_sequence_contiguous": True,
            "run_fence_preserved": True,
            "run_fence_token": claim.run.fence_token,
            "connection_bound": 2,
            "observed_application_connections": connection_count,
            "pool_stats": store.pool_stats,
            "learning_ledger_streaming_replication": True,
            "learning_acknowledged_state_preserved": True,
            "learning_existing_pool_reconnected": True,
            "learning_fresh_pool_verified": True,
            "learning_post_promotion_mutation": CandidateState.QUARANTINED.value,
            "learning_event_history_contiguous": True,
            "learning_no_writer_error": learning_no_writer_error.code,
            "learning_no_writer_error_retryable": learning_no_writer_error.retryable,
            "learning_no_writer_failure_seconds": round(
                learning_no_writer_failure_seconds,
                3,
            ),
            "learning_reconnect_attempts": learning_reconnect_attempts,
            "learning_reconnect_error_codes": learning_reconnect_errors,
            "learning_connection_bound": 2,
            "learning_observed_application_connections": learning_connection_count,
            "learning_pool_stats": learning.pool_stats,
            "split_brain_certification": False,
            "external_fencing_required": True,
            "open_production_gates": [
                "deployment-specific external fencing and automatic failover control plane",
                "unplanned-loss and synchronous-replication RPO policy",
                "network partition and split-brain fault injection",
                "production connection, memory, load, and timed RTO budgets",
                "old-primary rewind/rejoin and repeated role rotation",
            ],
        }
    finally:
        if learning is not None:
            learning.close()
        if artifact_directory is not None:
            artifact_directory.cleanup()
        store.close()


def _arguments() -> ProbeArguments:
    parser = argparse.ArgumentParser(
        description=(
            "Create an owned disposable PostgreSQL 17 primary/standby pair, prove lag "
            "and catch-up, fence the old primary, promote, and verify Agnoclaw recovery."
        )
    )
    parser.add_argument("--allow-topology-create", action="store_true")
    parser.add_argument("--image", default=POSTGRES_IMAGE)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--outage-timeout", type=float, default=1.0)
    parsed = parser.parse_args()
    return ProbeArguments(
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
    names = _resource_names(uuid4().hex[:16])
    owned = OwnedResources()
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
        cleanup = _cleanup_topology(owned, timeout=args.timeout_seconds)
        pool_logger.setLevel(prior_pool_level)
    if primary_failure is not None:
        if cleanup.failures:
            primary_failure.add_note("; ".join(cleanup.failures))
        raise primary_failure.with_traceback(primary_failure.__traceback__)
    if cleanup.failures:
        raise RuntimeError("PostgreSQL failover cleanup failed: " + "; ".join(cleanup.failures))
    if output is None:
        raise AssertionError("PostgreSQL failover probe completed without a result")
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
