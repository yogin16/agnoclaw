#!/usr/bin/env python3
"""Prove mTLS, exact-key RBAC, and quorum loss for writer authority."""

from __future__ import annotations

import argparse
import base64
import json
import re
import shutil
import ssl
import subprocess
import tempfile
import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import httpx

from agnoclaw.runtime import (
    ETCD_POSTGRES_WRITER_AUTHORITY_SCHEMA,
    EtcdGatewayCredentials,
    EtcdPostgresWriterAuthority,
    PostgresWriterAuthorityError,
)

ETCD_IMAGE = (
    "quay.io/coreos/etcd@sha256:"
    "dfd3941bf6ced5fdb700f9b2d98b22b7bca7ceee13aec16224f93ff30d9a59c4"
)
RESOURCE_PREFIX = "agnoclaw-etcd-secure-"
AUTHORITY_ID = "agnoclaw-secure-quorum-probe"
AUTHORITY_KEY = "/agnoclaw/postgres/secure-quorum/writer"
ADJACENT_KEY = "/agnoclaw/postgres/secure-quorum/other"
_RESOURCE_RE = re.compile(r"^agnoclaw-etcd-secure-[0-9a-f]{16}(?:-(?:m[0-2]|net))?$")
_IMAGE_RE = re.compile(r"^(?:quay\.io/coreos|gcr\.io/etcd-development)/etcd@sha256:[0-9a-f]{64}$")
_IDENTITY_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")


@dataclass(frozen=True)
class ProbeArguments:
    allow_topology_create: bool
    image: str
    timeout_seconds: float
    lease_ttl_seconds: int


@dataclass(frozen=True)
class ResourceNames:
    probe_id: str
    network: str
    members: tuple[str, str, str]


@dataclass
class OwnedResources:
    containers: list[str]
    networks: list[str]


@dataclass(frozen=True)
class CleanupResult:
    resources_removed: tuple[str, ...]
    failures: tuple[str, ...]


@dataclass(frozen=True)
class CertificatePaths:
    ca: Path
    certificates: dict[str, Path]
    keys: dict[str, Path]
    member_mounts: dict[str, Path]


def _validate_arguments(args: ProbeArguments) -> None:
    if not args.allow_topology_create:
        raise ValueError("--allow-topology-create is required")
    if not _IMAGE_RE.fullmatch(args.image):
        raise ValueError("--image must be an immutable official etcd digest")
    if not 20 <= args.timeout_seconds <= 180:
        raise ValueError("--timeout must be between 20 and 180 seconds")
    if not 10 <= args.lease_ttl_seconds <= 30:
        raise ValueError("--lease-ttl must be between 10 and 30 seconds")


def _resource_names(probe_id: str) -> ResourceNames:
    if not re.fullmatch(r"[0-9a-f]{16}", probe_id):
        raise ValueError("probe ID must be exactly 16 lowercase hexadecimal characters")
    stem = f"{RESOURCE_PREFIX}{probe_id}"
    names = ResourceNames(
        probe_id=probe_id,
        network=f"{stem}-net",
        members=(f"{stem}-m0", f"{stem}-m1", f"{stem}-m2"),
    )
    if not _RESOURCE_RE.fullmatch(names.network) or not all(
        _RESOURCE_RE.fullmatch(member) for member in names.members
    ):
        raise AssertionError("generated resource name escaped the owned namespace")
    return names


def _run(
    command: list[str],
    *,
    timeout: float,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            input=input_text,
            timeout=timeout,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        action = command[1] if len(command) > 1 else command[0]
        raise RuntimeError(f"secure-quorum action {action!r} failed") from exc


def _docker(*arguments: str, timeout: float) -> subprocess.CompletedProcess[str]:
    return _run(["docker", *arguments], timeout=timeout)


def _openssl(*arguments: str, timeout: float) -> subprocess.CompletedProcess[str]:
    return _run(["openssl", *arguments], timeout=timeout)


def _certificate(
    directory: Path,
    *,
    ca: Path,
    ca_key: Path,
    name: str,
    serial: int,
    server_names: tuple[str, ...] = (),
    loopback_ip: bool = False,
    common_name: bool = True,
) -> tuple[Path, Path]:
    if not _IDENTITY_RE.fullmatch(name):
        raise ValueError("certificate identity is outside the probe grammar")
    key = directory / f"{name}.key"
    csr = directory / f"{name}.csr"
    certificate = directory / f"{name}.crt"
    extensions = ["extendedKeyUsage=clientAuth"]
    if server_names or loopback_ip:
        if not all(_IDENTITY_RE.fullmatch(value) for value in server_names):
            raise ValueError("server certificate name is outside the probe grammar")
        extensions[0] = "extendedKeyUsage=serverAuth,clientAuth"
        subject_names = [*(f"DNS:{value}" for value in server_names)]
        if loopback_ip:
            subject_names.extend(("DNS:localhost", "IP:127.0.0.1"))
        extensions.append("subjectAltName=" + ",".join(subject_names))
    subject = f"/CN={name}" if common_name else f"/O=agnoclaw-secure-probe/OU={name}"
    request = [
        "req",
        "-new",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        str(key),
        "-out",
        str(csr),
        "-subj",
        subject,
    ]
    for extension in extensions:
        request.extend(("-addext", extension))
    _openssl(*request, timeout=15)
    _openssl(
        "x509",
        "-req",
        "-in",
        str(csr),
        "-CA",
        str(ca),
        "-CAkey",
        str(ca_key),
        "-set_serial",
        str(serial),
        "-days",
        "1",
        "-sha256",
        "-copy_extensions",
        "copy",
        "-out",
        str(certificate),
        timeout=15,
    )
    key.chmod(0o600)
    certificate.chmod(0o644)
    csr.unlink()
    return certificate, key


def _certificates(directory: Path, names: ResourceNames) -> CertificatePaths:
    directory.mkdir(mode=0o700)
    issuer = directory / "issuer"
    credentials = directory / "credentials"
    member_mount_root = directory / "members"
    issuer.mkdir(mode=0o700)
    credentials.mkdir(mode=0o700)
    member_mount_root.mkdir(mode=0o700)
    ca = credentials / "ca.crt"
    ca_key = issuer / "ca.key"
    _openssl(
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        str(ca_key),
        "-out",
        str(ca),
        "-subj",
        f"/CN=agnoclaw-secure-ca-{names.probe_id}",
        "-days",
        "1",
        "-sha256",
        "-addext",
        "basicConstraints=critical,CA:TRUE",
        "-addext",
        "keyUsage=critical,keyCertSign,cRLSign",
        timeout=15,
    )
    ca_key.chmod(0o600)
    ca.chmod(0o644)
    certificates: dict[str, Path] = {}
    keys: dict[str, Path] = {}
    serial = 1
    for member in names.members:
        client_identity = f"{member}-client"
        certificate, key = _certificate(
            credentials,
            ca=ca,
            ca_key=ca_key,
            name=client_identity,
            serial=serial,
            server_names=(member,),
            loopback_ip=True,
        )
        certificates[client_identity] = certificate
        keys[client_identity] = key
        serial += 1
        peer_identity = f"{member}-peer"
        certificate, key = _certificate(
            credentials,
            ca=ca,
            ca_key=ca_key,
            name=peer_identity,
            serial=serial,
            server_names=(member,),
        )
        certificates[peer_identity] = certificate
        keys[peer_identity] = key
        serial += 1
    for serial, identity in enumerate(
        (
            "root",
            "gateway-root",
            "gateway-controller",
            "gateway-reader",
            "gateway-denied",
        ),
        start=10,
    ):
        certificate, key = _certificate(
            credentials,
            ca=ca,
            ca_key=ca_key,
            name=identity,
            serial=serial,
            common_name=identity == "root",
        )
        certificates[identity] = certificate
        keys[identity] = key
    member_mounts: dict[str, Path] = {}
    for member in names.members:
        mount = member_mount_root / member
        mount.mkdir(mode=0o700)
        identities = (f"{member}-client", f"{member}-peer")
        for source in (
            ca,
            *(certificates[identity] for identity in identities),
            *(keys[identity] for identity in identities),
        ):
            target = mount / source.name
            shutil.copy2(source, target)
            target.chmod(0o600 if source.suffix == ".key" else 0o644)
        member_mounts[member] = mount
    root_member_mount = member_mounts[names.members[0]]
    for source in (certificates["root"], keys["root"]):
        target = root_member_mount / source.name
        shutil.copy2(source, target)
        target.chmod(0o600 if source == keys["root"] else 0o644)
    return CertificatePaths(
        ca=ca,
        certificates=certificates,
        keys=keys,
        member_mounts=member_mounts,
    )


def _member_arguments(
    names: ResourceNames,
    certificates: CertificatePaths,
    *,
    member: str,
) -> list[str]:
    if member not in names.members:
        raise ValueError("member is outside the owned topology")
    cluster = ",".join(f"{name}=https://{name}:2380" for name in names.members)
    client_identity = f"{member}-client"
    peer_identity = f"{member}-peer"
    cert = f"/certs/{certificates.certificates[client_identity].name}"
    key = f"/certs/{certificates.keys[client_identity].name}"
    peer_cert = f"/certs/{certificates.certificates[peer_identity].name}"
    peer_key = f"/certs/{certificates.keys[peer_identity].name}"
    return [
        "/usr/local/bin/etcd",
        "--name",
        member,
        "--data-dir",
        "/tmp/etcd-data",
        "--listen-client-urls",
        "https://0.0.0.0:2379",
        "--advertise-client-urls",
        f"https://{member}:2379",
        "--listen-peer-urls",
        "https://0.0.0.0:2380",
        "--initial-advertise-peer-urls",
        f"https://{member}:2380",
        "--initial-cluster",
        cluster,
        "--initial-cluster-token",
        f"agnoclaw-{names.probe_id}",
        "--initial-cluster-state",
        "new",
        "--cert-file",
        cert,
        "--key-file",
        key,
        "--client-cert-auth=true",
        "--trusted-ca-file",
        "/certs/ca.crt",
        "--peer-cert-file",
        peer_cert,
        "--peer-key-file",
        peer_key,
        "--peer-client-cert-auth=true",
        "--peer-trusted-ca-file",
        "/certs/ca.crt",
        "--tls-min-version",
        "TLS1.2",
        "--strict-reconfig-check=true",
        "--pre-vote=true",
        "--log-level",
        "warn",
    ]


def _published_port(container: str, *, timeout: float) -> int:
    if not _RESOURCE_RE.fullmatch(container):
        raise ValueError("container is outside the owned namespace")
    try:
        result = _docker("port", container, "2379/tcp", timeout=timeout)
    except RuntimeError as exc:
        try:
            state = _docker(
                "inspect",
                "--format",
                "{{.State.Status}} exit={{.State.ExitCode}} error={{.State.Error}}",
                container,
                timeout=timeout,
            ).stdout.strip()
        except RuntimeError:
            state = "unavailable"
        try:
            logs_result = _docker("logs", "--tail", "20", container, timeout=timeout)
            logs = (logs_result.stdout + logs_result.stderr).strip()[-4_096:]
        except RuntimeError:
            logs = "unavailable"
        raise RuntimeError(
            f"secure etcd member has no published client port: {state}; logs={logs}"
        ) from exc
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) != 1 or not lines[0].startswith("127.0.0.1:"):
        raise RuntimeError("member must publish exactly one IPv4 loopback client port")
    try:
        port = int(lines[0].rsplit(":", 1)[1])
    except ValueError as exc:
        raise RuntimeError("member published an invalid client port") from exc
    if not 1 <= port <= 65_535:
        raise RuntimeError("member published an invalid client port")
    return port


def _client(
    certificates: CertificatePaths,
    identity: str,
) -> httpx.Client:
    transport_identity = f"gateway-{identity.removeprefix('agnoclaw-')}"
    context = ssl.create_default_context(cafile=str(certificates.ca))
    context.load_cert_chain(
        certfile=str(certificates.certificates[transport_identity]),
        keyfile=str(certificates.keys[transport_identity]),
    )
    return httpx.Client(verify=context, trust_env=False)


def _wait_ready(endpoint: str, client: httpx.Client, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_failure = "no response"
    while time.monotonic() < deadline:
        try:
            response = client.get(f"{endpoint}/health", timeout=0.75)
            if response.status_code == 200 and response.json().get("health") in {
                "true",
                True,
            }:
                return
            last_failure = f"HTTP {response.status_code}"
        except (httpx.HTTPError, ValueError) as exc:
            last_failure = f"{type(exc).__name__}: {str(exc)[:512]}"
        time.sleep(0.1)
    raise RuntimeError(
        "secure etcd member did not become ready before the deadline: " + last_failure
    )


def _topology_diagnostics(names: ResourceNames, *, timeout: float) -> str:
    diagnostics: list[str] = []
    for member in names.members:
        try:
            state = _docker(
                "inspect",
                "--format",
                "{{.State.Status}} exit={{.State.ExitCode}} error={{.State.Error}}",
                member,
                timeout=timeout,
            ).stdout.strip()
        except RuntimeError:
            state = "unavailable"
        try:
            result = _docker("logs", "--tail", "30", member, timeout=timeout)
            logs = (result.stdout + result.stderr).strip()[-6_144:]
        except RuntimeError:
            logs = "unavailable"
        diagnostics.append(f"{member}: {state}; logs={logs}")
    return " | ".join(diagnostics)


def _post(
    client: httpx.Client,
    endpoint: str,
    path: str,
    payload: dict[str, object],
    *,
    timeout: float,
) -> dict[str, object]:
    try:
        response = client.post(f"{endpoint}{path}", json=payload, timeout=timeout)
        if response.is_error:
            try:
                error_payload = response.json()
            except ValueError:
                error_payload = {}
            error_code = (
                error_payload.get("code") if isinstance(error_payload, dict) else None
            )
            error_name = None
            if isinstance(error_payload, dict):
                error_name = error_payload.get("error") or error_payload.get("message")
            safe_error = (
                error_name
                if isinstance(error_name, str)
                and len(error_name) <= 256
                and all(32 <= ord(char) < 127 for char in error_name)
                else "invalid_error_body"
            )
            safe_shape = (
                sorted(str(key)[:64] for key in error_payload)
                if isinstance(error_payload, dict)
                else []
            )
            safe_body = response.text[:512]
            if not all(char in "\n\r\t" or 32 <= ord(char) < 127 for char in safe_body):
                safe_body = "non_ascii_error_body"
            raise RuntimeError(
                f"secure etcd HTTP {response.status_code} code={error_code!r} "
                f"error={safe_error!r} fields={safe_shape!r} body={safe_body!r}"
            )
        value = response.json()
    except RuntimeError:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeError("secure etcd request failed") from exc
    if not isinstance(value, dict):
        raise RuntimeError("secure etcd returned an invalid object")
    return value


def _etcdctl(
    names: ResourceNames,
    certificates: CertificatePaths,
    *arguments: str,
    identity: str = "root",
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    member = names.members[0]
    return _docker(
        "exec",
        member,
        "/usr/local/bin/etcdctl",
        f"--endpoints=https://{member}:2379",
        "--cacert=/certs/ca.crt",
        f"--cert=/certs/{certificates.certificates[identity].name}",
        f"--key=/certs/{certificates.keys[identity].name}",
        "--dial-timeout=2s",
        "--command-timeout=5s",
        *arguments,
        timeout=timeout,
    )


def _configure_rbac(names: ResourceNames, certificates: CertificatePaths) -> None:
    run = lambda *args, identity="root": _etcdctl(  # noqa: E731 - compact exact sequence
        names,
        certificates,
        *args,
        identity=identity,
        timeout=8,
    )
    run("user", "grant-role", "root", "root")
    run("role", "add", "agnoclaw-authority-controller")
    run(
        "role",
        "grant-permission",
        "agnoclaw-authority-controller",
        "readwrite",
        AUTHORITY_KEY,
    )
    run("user", "grant-role", "agnoclaw-controller", "agnoclaw-authority-controller")
    run("role", "add", "agnoclaw-authority-reader")
    run(
        "role",
        "grant-permission",
        "agnoclaw-authority-reader",
        "read",
        AUTHORITY_KEY,
    )
    run("user", "grant-role", "agnoclaw-reader", "agnoclaw-authority-reader")
    run("auth", "enable")


def _create_users(
    client: httpx.Client,
    endpoint: str,
    passwords: dict[str, str],
) -> None:
    for identity, password in passwords.items():
        _post(
            client,
            endpoint,
            "/v3/auth/user/add",
            {"name": identity, "password": password},
            timeout=3,
        )


def _authenticate(
    client: httpx.Client,
    endpoint: str,
    *,
    identity: str,
    password: str,
) -> None:
    payload = _post(
        client,
        endpoint,
        "/v3/auth/authenticate",
        {"name": identity, "password": password},
        timeout=2,
    )
    token = payload.get("token")
    if (
        not isinstance(token, str)
        or not 1 <= len(token) <= 8_192
        or any(ord(char) < 33 or ord(char) > 126 for char in token)
    ):
        raise RuntimeError("secure etcd returned an invalid authentication token")
    client.headers["Authorization"] = token


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


def _set_authority(
    client: httpx.Client,
    endpoint: str,
    *,
    server_id: str,
    ttl: int,
) -> None:
    lease = _post(
        client,
        endpoint,
        "/v3/lease/grant",
        {"TTL": ttl},
        timeout=2,
    ).get("ID")
    if not isinstance(lease, str) or not lease.isdecimal():
        raise RuntimeError("secure etcd returned an invalid lease ID")
    _post(
        client,
        endpoint,
        "/v3/kv/put",
        {
            "key": base64.b64encode(AUTHORITY_KEY.encode()).decode(),
            "value": _record(server_id),
            "lease": lease,
        },
        timeout=2,
    )


def _denial(provider: EtcdPostgresWriterAuthority) -> tuple[str, float]:
    started = time.monotonic()
    try:
        provider.current_grant(timeout_seconds=1)
    except PostgresWriterAuthorityError as exc:
        reason = str((exc.details or {}).get("reason", ""))
        if not reason:
            raise RuntimeError("authority denial omitted its safe reason") from exc
        return reason, time.monotonic() - started
    raise RuntimeError("quorum-lost authority unexpectedly admitted access")


def _expect_transport_rejection(endpoint: str, certificates: CertificatePaths) -> str:
    try:
        with httpx.Client(verify=str(certificates.ca), trust_env=False) as client:
            client.post(
                f"{endpoint}/v3/kv/range",
                json={"key": base64.b64encode(AUTHORITY_KEY.encode()).decode()},
                timeout=2,
            )
    except httpx.HTTPError as exc:
        return type(exc).__name__
    raise RuntimeError("etcd accepted a client without a certificate")


def _expect_permission_denied(
    client: httpx.Client,
    endpoint: str,
    *,
    path: str,
    payload: dict[str, object],
) -> int:
    response = client.post(f"{endpoint}{path}", json=payload, timeout=2)
    try:
        body = response.json()
    except ValueError as exc:
        raise RuntimeError("etcd RBAC returned an invalid denial body") from exc
    if (
        response.status_code != 403
        or not isinstance(body, dict)
        or body.get("code") not in {7, "7"}
        or body.get("message") != "etcdserver: permission denied"
    ):
        raise RuntimeError("etcd RBAC did not return a bounded client denial")
    return response.status_code


def _start_topology(
    args: ProbeArguments,
    names: ResourceNames,
    certificates: CertificatePaths,
    owned: OwnedResources,
) -> tuple[str, ...]:
    _docker(
        "network",
        "create",
        "--label",
        f"agnoclaw.probe={names.probe_id}",
        names.network,
        timeout=10,
    )
    owned.networks.append(names.network)
    for member in names.members:
        cert_mount = (
            "type=bind,source="
            f"{certificates.member_mounts[member]},destination=/certs,readonly"
        )
        _docker(
            "run",
            "--detach",
            "--name",
            member,
            "--label",
            f"agnoclaw.probe={names.probe_id}",
            "--network",
            names.network,
            "--hostname",
            member,
            "--publish",
            "127.0.0.1::2379",
            "--mount",
            cert_mount,
            args.image,
            *_member_arguments(names, certificates, member=member),
            timeout=15,
        )
        owned.containers.append(member)
    return tuple(
        f"https://127.0.0.1:{_published_port(member, timeout=5)}"
        for member in names.members
    )


def _probe(
    args: ProbeArguments,
    names: ResourceNames,
    owned: OwnedResources,
    certificate_directory: Path,
) -> dict[str, object]:
    started = time.monotonic()
    certificates = _certificates(certificate_directory, names)
    endpoints = _start_topology(args, names, certificates, owned)
    with ExitStack() as stack:
        root_client = stack.enter_context(_client(certificates, "root"))
        try:
            for endpoint in endpoints:
                _wait_ready(endpoint, root_client, timeout=args.timeout_seconds)
        except RuntimeError as exc:
            raise RuntimeError(
                f"secure etcd topology failed readiness: {exc}; "
                + _topology_diagnostics(names, timeout=5)
            ) from exc
        status = _post(
            root_client,
            endpoints[0],
            "/v3/maintenance/status",
            {},
            timeout=2,
        )
        header = status.get("header")
        if not isinstance(header, dict) or not isinstance(header.get("cluster_id"), str):
            raise RuntimeError("secure etcd status omitted its cluster identity")
        members = json.loads(
            _etcdctl(
                names,
                certificates,
                "member",
                "list",
                "--write-out=json",
                timeout=8,
            ).stdout
        )
        if not isinstance(members, dict) or len(members.get("members", [])) != 3:
            raise RuntimeError("secure etcd did not report exactly three voting members")
        unauthenticated_rejection = _expect_transport_rejection(endpoints[0], certificates)
        passwords = {
            identity: uuid4().hex + uuid4().hex
            for identity in (
                "root",
                "agnoclaw-controller",
                "agnoclaw-reader",
                "agnoclaw-denied",
            )
        }
        _create_users(root_client, endpoints[0], passwords)
        _configure_rbac(names, certificates)
        controller_client = stack.enter_context(
            _client(certificates, "agnoclaw-controller")
        )
        reader_client = stack.enter_context(_client(certificates, "agnoclaw-reader"))
        denied_client = stack.enter_context(_client(certificates, "agnoclaw-denied"))
        _authenticate(
            controller_client,
            endpoints[0],
            identity="agnoclaw-controller",
            password=passwords["agnoclaw-controller"],
        )
        _authenticate(
            reader_client,
            endpoints[0],
            identity="agnoclaw-reader",
            password=passwords["agnoclaw-reader"],
        )
        _authenticate(
            denied_client,
            endpoints[0],
            identity="agnoclaw-denied",
            password=passwords["agnoclaw-denied"],
        )

        encoded_key = base64.b64encode(AUTHORITY_KEY.encode()).decode()
        adjacent_key = base64.b64encode(ADJACENT_KEY.encode()).decode()
        denied_read_status = _expect_permission_denied(
            denied_client,
            endpoints[0],
            path="/v3/kv/range",
            payload={"key": encoded_key},
        )
        reader_write_status = _expect_permission_denied(
            reader_client,
            endpoints[0],
            path="/v3/kv/put",
            payload={"key": encoded_key, "value": _record("postgres-forbidden")},
        )
        adjacent_read_status = _expect_permission_denied(
            reader_client,
            endpoints[0],
            path="/v3/kv/range",
            payload={"key": adjacent_key},
        )

        _post(
            controller_client,
            endpoints[0],
            "/v3/kv/put",
            {"key": encoded_key, "value": _record("postgres-rbac-check")},
            timeout=2,
        )
        positive_range = _post(
            reader_client,
            endpoints[0],
            "/v3/kv/range",
            {"key": encoded_key, "serializable": False},
            timeout=2,
        )
        if positive_range.get("count") != "1":
            raise RuntimeError("exact-key reader did not observe the controller write")

        _set_authority(
            controller_client,
            endpoints[0],
            server_id="postgres-a",
            ttl=args.lease_ttl_seconds,
        )
        authority_client = stack.enter_context(
            _client(certificates, "agnoclaw-reader")
        )
        authority = EtcdPostgresWriterAuthority(
            endpoint=endpoints[0],
            key=AUTHORITY_KEY,
            authority_id=AUTHORITY_ID,
            cluster_id=header["cluster_id"],
            http_client=authority_client,
            ttl_uncertainty_seconds=0.25,
            gateway_credentials=EtcdGatewayCredentials(
                endpoint=endpoints[0],
                username="agnoclaw-reader",
                password=passwords["agnoclaw-reader"],
            ),
        )
        initial = authority.current_grant(timeout_seconds=2)

        _docker("stop", "--timeout", "1", names.members[1], timeout=5)
        one_member_down = authority.current_grant(timeout_seconds=2)
        if one_member_down.fence_token != initial.fence_token:
            raise RuntimeError("authority revision changed during one-member loss")

        _docker("stop", "--timeout", "1", names.members[2], timeout=5)
        quorum_reason, quorum_denial_seconds = _denial(authority)
        if quorum_reason not in {"etcd_timeout", "etcd_unavailable"}:
            raise RuntimeError("majority loss returned an unexpected denial reason")

        _docker("start", names.members[1], timeout=5)
        recovered_endpoint = (
            f"https://127.0.0.1:{_published_port(names.members[1], timeout=5)}"
        )
        with _client(certificates, "root") as readiness_client:
            _wait_ready(
                recovered_endpoint,
                readiness_client,
                timeout=min(args.timeout_seconds, 20),
            )
        controller_client = stack.enter_context(
            _client(certificates, "agnoclaw-controller")
        )
        _authenticate(
            controller_client,
            recovered_endpoint,
            identity="agnoclaw-controller",
            password=passwords["agnoclaw-controller"],
        )
        _set_authority(
            controller_client,
            recovered_endpoint,
            server_id="postgres-b",
            ttl=args.lease_ttl_seconds,
        )
        recovered_reader_client = stack.enter_context(
            _client(certificates, "agnoclaw-reader")
        )
        recovered_authority = EtcdPostgresWriterAuthority(
            endpoint=recovered_endpoint,
            key=AUTHORITY_KEY,
            authority_id=AUTHORITY_ID,
            cluster_id=header["cluster_id"],
            http_client=recovered_reader_client,
            ttl_uncertainty_seconds=0.25,
            gateway_credentials=EtcdGatewayCredentials(
                endpoint=recovered_endpoint,
                username="agnoclaw-reader",
                password=passwords["agnoclaw-reader"],
            ),
        )
        recovered = recovered_authority.current_grant(timeout_seconds=2)
        if recovered.server_id != "postgres-b" or recovered.fence_token <= initial.fence_token:
            raise RuntimeError("authority did not recover with a fresh fenced generation")

        _docker("start", names.members[2], timeout=5)
        restored_endpoints = (
            endpoints[0],
            recovered_endpoint,
            f"https://127.0.0.1:{_published_port(names.members[2], timeout=5)}",
        )
        for endpoint in restored_endpoints:
            with _client(certificates, "root") as readiness_client:
                _wait_ready(
                    endpoint,
                    readiness_client,
                    timeout=min(args.timeout_seconds, 20),
                )
        return {
            "status": "passed",
            "scope": "owned_three_member_mtls_rbac_quorum_authority_probe",
            "production_certification": False,
            "etcd_image_digest": args.image,
            "etcd_version": str(status.get("version", "")),
            "cluster_members": 3,
            "unique_member_certificates": True,
            "ca_private_key_mounted": False,
            "client_private_keys_isolated": True,
            "client_tls_verified": True,
            "peer_tls_verified": True,
            "minimum_tls_version": "TLS1.2",
            "unauthenticated_client_rejected": True,
            "unauthenticated_rejection": unauthenticated_rejection,
            "rbac_enabled": True,
            "gateway_certificate_cn_authentication": False,
            "gateway_token_authentication": True,
            "first_party_gateway_credentials": True,
            "token_reauthentication_after_endpoint_failover": True,
            "controller_exact_key_readwrite": True,
            "reader_exact_key_read_only": True,
            "reader_write_denial_status": reader_write_status,
            "reader_adjacent_key_denial_status": adjacent_read_status,
            "unprivileged_read_denial_status": denied_read_status,
            "one_member_loss_tolerated": True,
            "majority_loss_failed_closed": True,
            "majority_loss_reason": quorum_reason,
            "majority_loss_denial_seconds": round(quorum_denial_seconds, 3),
            "quorum_recovered": True,
            "fence_advanced_after_recovery": True,
            "fence_tokens": [initial.fence_token, recovered.fence_token],
            "strict_reconfiguration_check": True,
            "pre_vote_enabled": True,
            "persistent_data_volumes": 0,
            "network_partition_certification": False,
            "controller_election_certification": False,
            "watchdog_or_stonith_certification": False,
            "production_rpo_rto_certification": False,
            "elapsed_seconds_before_cleanup": round(time.monotonic() - started, 3),
        }


def _cleanup(owned: OwnedResources, *, timeout: float) -> CleanupResult:
    removed: list[str] = []
    failures: list[str] = []
    for container in reversed(owned.containers):
        if not _RESOURCE_RE.fullmatch(container):
            failures.append("refused container cleanup outside the owned namespace")
            continue
        try:
            _docker("rm", "--force", container, timeout=min(timeout, 15))
            removed.append(container)
        except BaseException as exc:
            failures.append(str(exc))
    for network in reversed(owned.networks):
        if not _RESOURCE_RE.fullmatch(network):
            failures.append("refused network cleanup outside the owned namespace")
            continue
        try:
            _docker("network", "rm", network, timeout=min(timeout, 15))
            removed.append(network)
        except BaseException as exc:
            failures.append(str(exc))
    return CleanupResult(resources_removed=tuple(removed), failures=tuple(failures))


def _arguments() -> ProbeArguments:
    parser = argparse.ArgumentParser(
        description="Prove secure three-member etcd authority quorum behavior."
    )
    parser.add_argument("--allow-topology-create", action="store_true")
    parser.add_argument("--image", default=ETCD_IMAGE)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--lease-ttl", type=int, default=15)
    parsed = parser.parse_args()
    return ProbeArguments(
        allow_topology_create=bool(parsed.allow_topology_create),
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
    names = _resource_names(uuid4().hex[:16])
    owned = OwnedResources(containers=[], networks=[])
    output: dict[str, object] | None = None
    primary_failure: BaseException | None = None
    started = time.monotonic()
    with tempfile.TemporaryDirectory(
        prefix=f"{RESOURCE_PREFIX}{names.probe_id}-",
        dir="/tmp",
    ) as raw:
        try:
            output = _probe(args, names, owned, Path(raw).resolve() / "certs")
        except BaseException as exc:
            primary_failure = exc
        cleanup = _cleanup(owned, timeout=args.timeout_seconds)
    if primary_failure is not None:
        if cleanup.failures:
            primary_failure.add_note("; ".join(cleanup.failures))
        raise primary_failure.with_traceback(primary_failure.__traceback__)
    if cleanup.failures:
        raise RuntimeError("secure etcd cleanup failed: " + "; ".join(cleanup.failures))
    if output is None:
        raise AssertionError("secure etcd probe returned no result")
    output["owned_resources_created"] = len(owned.containers) + len(owned.networks)
    output["owned_resources_removed"] = len(cleanup.resources_removed)
    output["cleanup_complete"] = True
    output["certificate_workspace_removed"] = True
    output["elapsed_seconds"] = round(time.monotonic() - started, 3)
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
