"""Hardened Docker evaluation policy, execution, and cleanup contracts."""

from __future__ import annotations

import asyncio
import json
import re
import sys
import textwrap
from pathlib import Path

import pytest

from agnoclaw import (
    DockerEvaluationImageError,
    DockerEvaluationPolicy,
    DockerEvaluationSubjectFactory,
    EvaluationCase,
    EvaluationSlice,
    ProcessEvaluationCleanupError,
)


def _case() -> EvaluationCase:
    return EvaluationCase(
        case_id="docker-case",
        slice=EvaluationSlice.HELD_IN,
        task_class="docker-contract",
        payload={"quality": 0.8},
    )


def _fake_docker(tmp_path: Path) -> Path:
    executable = tmp_path / "fake-docker"
    root = repr(str(tmp_path))
    executable.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent(
            f"""
            import json
            import subprocess
            import sys
            from pathlib import Path

            ROOT = Path({root})
            args = sys.argv[1:]
            with (ROOT / "calls.jsonl").open("a", encoding="utf-8") as log:
                log.write(json.dumps(args) + "\\n")

            def state_path(name):
                return ROOT / (name + ".json")

            if args[:2] == ["container", "inspect"]:
                path = state_path(args[-1])
                if not path.exists():
                    raise SystemExit(1)
                print(json.loads(path.read_text(encoding="utf-8"))["owner"])
                raise SystemExit(0)

            if args[:2] == ["image", "inspect"]:
                volumes = {{"/declared": {{}}}} if (ROOT / "image-volumes").exists() else None
                print(json.dumps(volumes))
                print(json.dumps("linux"))
                print(json.dumps("amd64"))
                raise SystemExit(0)

            if args[:3] == ["container", "rm", "--force"]:
                path = state_path(args[-1])
                if not path.exists():
                    raise SystemExit(1)
                path.unlink()
                raise SystemExit(0)

            if args and args[0] == "version":
                print("fake-1")
                raise SystemExit(0)

            if not args or args[0] != "run":
                raise SystemExit(2)

            value_options = {{
                "--pull", "--platform", "--name", "--label", "--network", "--cap-drop",
                "--security-opt", "--pids-limit", "--memory", "--memory-swap",
                "--cpus", "--user", "--workdir", "--tmpfs", "--ipc",
                "--cgroupns", "--log-driver", "--hostname", "--ulimit", "--entrypoint",
            }}
            flag_options = {{
                "--rm", "--interactive", "--read-only", "--init", "--no-healthcheck"
            }}
            name = None
            owner = None
            entrypoint = None
            index = 1
            while index < len(args):
                token = args[index]
                if token in value_options:
                    value = args[index + 1]
                    if token == "--name":
                        name = value
                    if token == "--label" and value.startswith(
                        "agnoclaw.evaluation.owner="
                    ):
                        owner = value.split("=", 1)[1]
                    if token == "--entrypoint":
                        entrypoint = value
                    index += 2
                    continue
                if token in flag_options:
                    index += 1
                    continue
                break
            if name is None or owner is None or entrypoint is None or index >= len(args):
                raise SystemExit(2)
            command = [entrypoint, *args[index + 1:]]
            path = state_path(name)
            path.write_text(json.dumps({{"owner": owner}}), encoding="utf-8")
            result = subprocess.run(
                command,
                input=sys.stdin.buffer.read(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                env={{}},
            )
            path.unlink(missing_ok=True)
            sys.stdout.buffer.write(result.stdout)
            sys.stderr.buffer.write(result.stderr)
            raise SystemExit(result.returncode)
            """
        ),
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable


def _worker_code() -> str:
    return (
        "import json,sys;"
        "r=json.load(sys.stdin);"
        "json.dump({'protocol_version':r['protocol_version'],"
        "'request_id':r['request_id'],'ok':True,"
        "'rollout':{'output':{'sandboxed':True},'tokens':1,'cost_usd':0.0}},"
        "sys.stdout)"
    )


def test_docker_policy_is_immutable_bounded_and_digest_comparable(tmp_path: Path) -> None:
    docker = _fake_docker(tmp_path)
    image = "sha256:" + "a" * 64
    policy = DockerEvaluationPolicy(image=image)
    baseline = DockerEvaluationSubjectFactory(
        docker,
        policy,
        (sys.executable, "-c", _worker_code()),
    )
    candidate = DockerEvaluationSubjectFactory(
        docker,
        policy,
        (sys.executable, "-c", _worker_code() + ";x=1"),
    )

    assert re.fullmatch(r"sha256:[0-9a-f]{64}", policy.digest)
    assert baseline.subject_isolation_digest == candidate.subject_isolation_digest
    assert baseline.subject_contract_digest != candidate.subject_contract_digest
    assert image not in repr(baseline)
    assert _worker_code() not in repr(baseline)
    assert DockerEvaluationPolicy(image=image, memory_bytes=1024 * 1024 * 1024).digest != (
        policy.digest
    )

    subject = baseline()
    command = subject._inner._config.command  # noqa: SLF001 - exact security argv contract
    for required in (
        "--pull",
        "--platform",
        "never",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "no-new-privileges=true",
        "seccomp=builtin",
        "--pids-limit",
        "--memory",
        "--memory-swap",
        "--cpus",
        "--user",
        "--tmpfs",
        "--ipc",
        "--cgroupns",
        "--init",
        "--no-healthcheck",
        "--log-driver",
        "--ulimit",
        "--entrypoint",
    ):
        assert required in command
    assert "--mount" not in command
    assert "--volume" not in command
    assert "--env" not in command

    with pytest.raises(ValueError, match="immutable sha256"):
        DockerEvaluationPolicy(image="python:latest")
    with pytest.raises(ValueError, match="non-root"):
        DockerEvaluationPolicy(image=image, user="0:0")
    with pytest.raises(ValueError, match="linux/amd64"):
        DockerEvaluationPolicy(image=image, platform="linux")
    with pytest.raises(ValueError, match="tmpfs_bytes"):
        DockerEvaluationPolicy(image=image, memory_bytes=16 * 1024 * 1024)
    with pytest.raises(ValueError, match="absolute"):
        DockerEvaluationSubjectFactory("docker", policy, ("worker",))


@pytest.mark.asyncio
async def test_docker_subject_executes_protocol_and_removes_success_container(
    tmp_path: Path,
) -> None:
    docker = _fake_docker(tmp_path)
    subject = DockerEvaluationSubjectFactory(
        docker,
        DockerEvaluationPolicy(image="sha256:" + "b" * 64),
        (sys.executable, "-c", _worker_code()),
    )()
    await subject.asetup()
    try:
        rollout = await subject(_case())
    finally:
        await subject.aclose()

    assert rollout.output == {"sandboxed": True}
    assert rollout.tokens == 1
    assert not (tmp_path / f"{subject._name}.json").exists()  # noqa: SLF001
    calls = [
        json.loads(line)
        for line in (tmp_path / "calls.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    run = next(call for call in calls if call and call[0] == "run")
    assert run[0] == "run"
    assert "--rm" in run
    assert "--network" in run
    assert "none" in run


@pytest.mark.asyncio
async def test_docker_subject_rejects_image_declared_writable_volumes(
    tmp_path: Path,
) -> None:
    docker = _fake_docker(tmp_path)
    (tmp_path / "image-volumes").write_text("present", encoding="utf-8")
    subject = DockerEvaluationSubjectFactory(
        docker,
        DockerEvaluationPolicy(image="sha256:" + "e" * 64),
        (sys.executable, "-c", _worker_code()),
    )()

    with pytest.raises(DockerEvaluationImageError, match="declare no writable volumes"):
        await subject.asetup()


@pytest.mark.asyncio
async def test_docker_subject_cancellation_proves_exact_owned_cleanup(tmp_path: Path) -> None:
    docker = _fake_docker(tmp_path)
    subject = DockerEvaluationSubjectFactory(
        docker,
        DockerEvaluationPolicy(image="sha256:" + "c" * 64),
        (sys.executable, "-c", "import time; time.sleep(60)"),
        terminate_grace_seconds=0.05,
    )()
    await subject.asetup()
    state = tmp_path / f"{subject._name}.json"  # noqa: SLF001
    task = asyncio.create_task(subject(_case()))
    for _ in range(200):
        if state.exists():
            break
        await asyncio.sleep(0.01)
    assert state.exists()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await subject.aclose()
    assert not state.exists()
    calls = [
        json.loads(line)
        for line in (tmp_path / "calls.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert any(call[:2] == ["container", "inspect"] for call in calls)
    assert any(call[:3] == ["container", "rm", "--force"] for call in calls)


@pytest.mark.asyncio
async def test_docker_cleanup_refuses_container_without_exact_owner_label(
    tmp_path: Path,
) -> None:
    docker = _fake_docker(tmp_path)
    subject = DockerEvaluationSubjectFactory(
        docker,
        DockerEvaluationPolicy(image="sha256:" + "d" * 64),
        (sys.executable, "-c", _worker_code()),
    )()
    await subject.asetup()
    state = tmp_path / f"{subject._name}.json"  # noqa: SLF001
    state.write_text(json.dumps({"owner": "another-owner"}), encoding="utf-8")
    subject._container_may_exist = True  # noqa: SLF001 - adversarial ownership fixture

    with pytest.raises(ProcessEvaluationCleanupError, match="ownership"):
        await subject.aclose()
    assert state.exists()
    state.unlink()
