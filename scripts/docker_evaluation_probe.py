#!/usr/bin/env python3
"""Run one explicit live proof of the hardened Docker evaluation policy."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

from agnoclaw import (
    DOCKER_EVALUATION_POLICY_VERSION,
    DockerEvaluationPolicy,
    EvaluationCase,
    EvaluationSlice,
    docker_evaluation_subject_factory,
)

_WORKER = r"""
import json
import os
import resource
import socket
import sys

request = json.load(sys.stdin)
sock = socket.socket()
sock.settimeout(0.2)
try:
    sock.connect(("1.1.1.1", 53))
    network_blocked = False
except OSError:
    network_blocked = True
finally:
    sock.close()

try:
    with open("/agnoclaw-forbidden", "w", encoding="utf-8") as handle:
        handle.write("forbidden")
    root_read_only = False
except OSError:
    root_read_only = True

with open("/tmp/agnoclaw-allowed", "w", encoding="utf-8") as handle:
    handle.write("ok")

status = {}
with open("/proc/self/status", encoding="utf-8") as handle:
    for line in handle:
        name, separator, value = line.partition(":")
        if separator and name in {"CapEff", "NoNewPrivs", "Seccomp"}:
            status[name] = value.strip()

output = {
    "uid": os.getuid(),
    "gid": os.getgid(),
    "network_blocked": network_blocked,
    "root_read_only": root_read_only,
    "tmpfs_writable": os.path.exists("/tmp/agnoclaw-allowed"),
    "nofile": resource.getrlimit(resource.RLIMIT_NOFILE)[0],
    "core": resource.getrlimit(resource.RLIMIT_CORE)[0],
    "cap_eff": status.get("CapEff"),
    "no_new_privileges": status.get("NoNewPrivs"),
    "seccomp": status.get("Seccomp"),
}
json.dump(
    {
        "protocol_version": request["protocol_version"],
        "request_id": request["request_id"],
        "ok": True,
        "rollout": {"output": output, "tokens": 1, "cost_usd": 0.0},
    },
    sys.stdout,
)
"""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one explicitly authorized temporary Docker containment probe.",
    )
    parser.add_argument(
        "--docker",
        help="Absolute Docker CLI path; defaults to the executable on PATH.",
    )
    parser.add_argument(
        "--image",
        required=True,
        help="Existing immutable sha256 image ID or repository digest reference.",
    )
    parser.add_argument("--memory-bytes", type=int, default=128 * 1024 * 1024)
    parser.add_argument("--platform", default="linux/amd64")
    parser.add_argument("--cpu-limit", type=float, default=1.0)
    parser.add_argument("--pids-limit", type=int, default=64)
    parser.add_argument("--nofile-limit", type=int, default=1024)
    parser.add_argument(
        "--allow-live-docker",
        action="store_true",
        help="Required acknowledgement that one temporary container will be created.",
    )
    return parser


async def _probe(arguments: argparse.Namespace) -> dict[str, Any]:
    docker = arguments.docker or shutil.which("docker")
    if docker is None:
        raise RuntimeError("Docker CLI was not found")
    docker_path = Path(docker).expanduser().resolve(strict=True)
    policy = DockerEvaluationPolicy(
        image=arguments.image,
        platform=arguments.platform,
        memory_bytes=arguments.memory_bytes,
        cpu_limit=arguments.cpu_limit,
        pids_limit=arguments.pids_limit,
        nofile_limit=arguments.nofile_limit,
    )
    factory = docker_evaluation_subject_factory(
        docker_path,
        policy,
        ("python3", "-c", _WORKER),
    )
    subject = factory()
    await subject.asetup()
    try:
        rollout = await subject(
            EvaluationCase(
                case_id="live-docker-policy",
                slice=EvaluationSlice.HELD_OUT,
                task_class="containment",
                payload={},
            )
        )
    finally:
        await subject.aclose()
    evidence = dict(rollout.output)
    expected_uid, expected_gid = (int(item) for item in policy.user.split(":"))
    expected = {
        "uid": expected_uid,
        "gid": expected_gid,
        "network_blocked": True,
        "root_read_only": True,
        "tmpfs_writable": True,
        "nofile": policy.nofile_limit,
        "core": 0,
        "cap_eff": "0000000000000000",
        "no_new_privileges": "1",
        "seccomp": "2",
    }
    failures = [name for name, value in expected.items() if evidence.get(name) != value]
    if failures:
        raise RuntimeError(
            "Docker evaluation containment proof failed: " + ",".join(sorted(failures))
        )
    return {
        "status": "passed",
        "policy_version": DOCKER_EVALUATION_POLICY_VERSION,
        "policy_digest": policy.digest,
        "subject_isolation_digest": factory.subject_isolation_digest,
        "evidence": evidence,
    }


def main() -> int:
    parser = _parser()
    arguments = parser.parse_args()
    if not arguments.allow_live_docker:
        parser.error("--allow-live-docker is required")
    print(json.dumps(asyncio.run(_probe(arguments)), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
