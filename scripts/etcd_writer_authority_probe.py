#!/usr/bin/env python3
"""Prove live etcd lease-backed PostgreSQL authority observations and expiry."""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import time
from dataclasses import dataclass
from uuid import uuid4

import httpx

from agnoclaw.runtime import (
    ETCD_POSTGRES_WRITER_AUTHORITY_SCHEMA,
    EtcdPostgresWriterAuthority,
    PostgresWriterAuthorityError,
)

ETCD_IMAGE = (
    "quay.io/coreos/etcd@sha256:"
    "dfd3941bf6ced5fdb700f9b2d98b22b7bca7ceee13aec16224f93ff30d9a59c4"
)
RESOURCE_PREFIX = "agnoclaw-etcd-authority-"
AUTHORITY_ID = "agnoclaw-live-etcd-probe"
KEY = "/agnoclaw/postgres/live-probe/writer"
_RESOURCE_RE = re.compile(r"^agnoclaw-etcd-authority-[0-9a-f]{16}$")
_IMAGE_RE = re.compile(r"^quay\.io/coreos/etcd@sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class ProbeArguments:
    allow_container_create: bool
    image: str
    timeout_seconds: float
    lease_ttl_seconds: int


def _validate_arguments(args: ProbeArguments) -> None:
    if not args.allow_container_create:
        raise ValueError("--allow-container-create is required")
    if not _IMAGE_RE.fullmatch(args.image):
        raise ValueError("--image must be the immutable official etcd digest")
    if not 10 <= args.timeout_seconds <= 180:
        raise ValueError("--timeout must be between 10 and 180 seconds")
    if not 3 <= args.lease_ttl_seconds <= 15:
        raise ValueError("--lease-ttl must be between 3 and 15 seconds")


def _docker(*arguments: str, timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["docker", *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        action = arguments[0] if arguments else "docker"
        raise RuntimeError(f"Docker action {action!r} failed") from exc


def _post(endpoint: str, path: str, payload: dict[str, object], *, timeout: float):
    try:
        with httpx.Client(trust_env=False) as client:
            response = client.post(
                f"{endpoint}{path}", json=payload, timeout=timeout
            )
            response.raise_for_status()
            value = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeError("live etcd request failed") from exc
    if not isinstance(value, dict):
        raise RuntimeError("live etcd returned an invalid object")
    return value


def _wait_ready(endpoint: str, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    with httpx.Client(trust_env=False) as client:
        while time.monotonic() < deadline:
            try:
                response = client.get(f"{endpoint}/health", timeout=0.5)
                if response.status_code == 200 and response.json().get("health") in {
                    "true",
                    True,
                }:
                    return
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(0.1)
    raise RuntimeError("live etcd did not become ready before the deadline")


def _published_port(container: str, *, timeout: float) -> int:
    result = _docker("port", container, "2379/tcp", timeout=timeout)
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) != 1 or not lines[0].startswith("127.0.0.1:"):
        raise RuntimeError("etcd must publish exactly one IPv4 loopback port")
    port = int(lines[0].rsplit(":", 1)[1])
    if not 1 <= port <= 65_535:
        raise RuntimeError("etcd published an invalid port")
    return port


def _record(server_id: str) -> str:
    raw = json.dumps(
        {
            "schema": ETCD_POSTGRES_WRITER_AUTHORITY_SCHEMA,
            "authority_id": AUTHORITY_ID,
            "server_id": server_id,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return base64.b64encode(raw).decode()


def _put(endpoint: str, *, lease_id: str, server_id: str, timeout: float) -> None:
    _post(
        endpoint,
        "/v3/kv/put",
        {
            "key": base64.b64encode(KEY.encode()).decode(),
            "value": _record(server_id),
            "lease": lease_id,
        },
        timeout=timeout,
    )


def _grant_lease(endpoint: str, *, ttl: int, timeout: float) -> str:
    result = _post(endpoint, "/v3/lease/grant", {"TTL": ttl}, timeout=timeout)
    lease_id = result.get("ID")
    if not isinstance(lease_id, str) or not lease_id.isdecimal():
        raise RuntimeError("live etcd returned an invalid lease ID")
    return lease_id


def _reason(provider: EtcdPostgresWriterAuthority) -> tuple[str, float]:
    started = time.monotonic()
    try:
        provider.current_grant(timeout_seconds=1)
    except PostgresWriterAuthorityError as exc:
        reason = str((exc.details or {}).get("reason", ""))
        if not reason:
            raise RuntimeError("authority denial omitted its safe reason") from exc
        return reason, time.monotonic() - started
    raise RuntimeError("live etcd authority unexpectedly admitted access")


def _probe(args: ProbeArguments, container: str) -> dict[str, object]:
    started = time.monotonic()
    _docker(
        "run",
        "--detach",
        "--name",
        container,
        "--publish",
        "127.0.0.1::2379",
        args.image,
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
        timeout=args.timeout_seconds,
    )
    endpoint = f"http://127.0.0.1:{_published_port(container, timeout=5)}"
    _wait_ready(endpoint, timeout=args.timeout_seconds)
    status = _post(endpoint, "/v3/maintenance/status", {}, timeout=2)
    header = status.get("header")
    if not isinstance(header, dict) or not isinstance(header.get("cluster_id"), str):
        raise RuntimeError("live etcd status omitted its cluster identity")
    provider = EtcdPostgresWriterAuthority(
        endpoint=endpoint,
        key=KEY,
        authority_id=AUTHORITY_ID,
        cluster_id=header["cluster_id"],
        allow_insecure_loopback=True,
        ttl_uncertainty_seconds=0.25,
    )
    try:
        lease_one = _grant_lease(
            endpoint, ttl=args.lease_ttl_seconds, timeout=2
        )
        _put(endpoint, lease_id=lease_one, server_id="postgres-a", timeout=2)
        grant_one = provider.current_grant(timeout_seconds=1)

        lease_two = _grant_lease(
            endpoint, ttl=args.lease_ttl_seconds, timeout=2
        )
        _put(endpoint, lease_id=lease_two, server_id="postgres-b", timeout=2)
        grant_two = provider.current_grant(timeout_seconds=1)
        if grant_two.fence_token <= grant_one.fence_token:
            raise RuntimeError("etcd modification revision did not advance")

        _post(endpoint, "/v3/lease/revoke", {"ID": lease_two}, timeout=2)
        revoked_reason, revoked_seconds = _reason(provider)
        if revoked_reason != "etcd_authority_absent":
            raise RuntimeError("revoked lease returned an unexpected denial reason")

        lease_three = _grant_lease(
            endpoint, ttl=args.lease_ttl_seconds, timeout=2
        )
        _put(endpoint, lease_id=lease_three, server_id="postgres-c", timeout=2)
        grant_three = provider.current_grant(timeout_seconds=1)
        expiry_deadline = time.monotonic() + args.lease_ttl_seconds + 5
        while True:
            try:
                provider.current_grant(timeout_seconds=1)
            except PostgresWriterAuthorityError as exc:
                expiry_reason = str((exc.details or {}).get("reason", ""))
                break
            if time.monotonic() >= expiry_deadline:
                raise RuntimeError("unrenewed authority lease did not expire")
            time.sleep(0.1)
        if expiry_reason not in {"etcd_authority_absent", "etcd_lease_expired"}:
            raise RuntimeError("expired lease returned an unexpected denial reason")

        _docker("stop", "--timeout", "1", container, timeout=5)
        unavailable_reason, unavailable_seconds = _reason(provider)
        if unavailable_reason not in {"etcd_unavailable", "etcd_timeout"}:
            raise RuntimeError("stopped etcd returned an unexpected denial reason")
        return {
            "status": "passed",
            "scope": "owned_single_node_live_etcd_authority_adapter_probe",
            "production_certification": False,
            "etcd_image_digest": args.image,
            "etcd_version": str(status.get("version", "")),
            "real_etcd_gateway": True,
            "linearizable_bracket_reads": True,
            "lease_backing_verified": True,
            "revision_fence_advanced": True,
            "fence_tokens": [
                grant_one.fence_token,
                grant_two.fence_token,
                grant_three.fence_token,
            ],
            "revocation_denied": True,
            "revocation_reason": revoked_reason,
            "revocation_denial_seconds": round(revoked_seconds, 3),
            "unrenewed_lease_expired": True,
            "expiry_reason": expiry_reason,
            "control_plane_loss_denied": True,
            "control_plane_loss_reason": unavailable_reason,
            "control_plane_loss_denial_seconds": round(unavailable_seconds, 3),
            "tls_and_rbac_certification": False,
            "etcd_quorum_loss_certification": False,
            "postgres_dual_writer_integration_certification": False,
            "elapsed_seconds_before_cleanup": round(time.monotonic() - started, 3),
        }
    finally:
        provider.close()


def _arguments() -> ProbeArguments:
    parser = argparse.ArgumentParser(
        description="Prove the first-party writer-authority adapter against live etcd."
    )
    parser.add_argument("--allow-container-create", action="store_true")
    parser.add_argument("--image", default=ETCD_IMAGE)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--lease-ttl", type=int, default=3)
    parsed = parser.parse_args()
    return ProbeArguments(
        allow_container_create=bool(parsed.allow_container_create),
        image=str(parsed.image),
        timeout_seconds=float(parsed.timeout),
        lease_ttl_seconds=int(parsed.lease_ttl),
    )


def main() -> int:
    args = _arguments()
    try:
        _validate_arguments(args)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    container = f"{RESOURCE_PREFIX}{uuid4().hex[:16]}"
    output: dict[str, object] | None = None
    primary_failure: BaseException | None = None
    started = time.monotonic()
    try:
        output = _probe(args, container)
    except BaseException as exc:
        primary_failure = exc
    cleanup_failure: BaseException | None = None
    try:
        if not _RESOURCE_RE.fullmatch(container):
            raise RuntimeError("refusing to remove a container outside the probe namespace")
        _docker("rm", "--force", container, timeout=min(args.timeout_seconds, 15))
    except BaseException as exc:
        cleanup_failure = exc
    if primary_failure is not None:
        if cleanup_failure is not None:
            primary_failure.add_note(str(cleanup_failure))
        raise primary_failure.with_traceback(primary_failure.__traceback__)
    if cleanup_failure is not None:
        raise cleanup_failure
    if output is None:
        raise AssertionError("live etcd probe returned no result")
    output["owned_resources_created"] = 1
    output["owned_resources_removed"] = 1
    output["cleanup_complete"] = True
    output["elapsed_seconds"] = round(time.monotonic() - started, 3)
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
