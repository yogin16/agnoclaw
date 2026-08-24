#!/usr/bin/env python3
"""Real-process recovery probes used by ADR-0001.

The stable probe kills an Agno 2.x Agent while its model request is in flight and
reports the durable session/run projection. The preview probe uses Agno 3's actual
Postgres queue contract to kill workers immediately before and after an external effect,
then demonstrates reclaim, attempt fencing, and effect ambiguity.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from agno.models.base import Model
from agno.models.response import ModelResponse


def _wait_for(path: Path, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise TimeoutError(f"timed out waiting for {path}")


@dataclass
class BlockingModel(Model):
    marker: str = ""

    def invoke(self, *args: Any, **kwargs: Any) -> ModelResponse:
        Path(self.marker).write_text("model-dispatched\n", encoding="utf-8")
        time.sleep(300)
        return ModelResponse(content="unreachable")

    async def ainvoke(self, *args: Any, **kwargs: Any) -> ModelResponse:
        return await asyncio.to_thread(self.invoke, *args, **kwargs)

    def invoke_stream(self, *args: Any, **kwargs: Any) -> Iterator[ModelResponse]:
        yield self.invoke(*args, **kwargs)

    async def ainvoke_stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[ModelResponse]:
        yield await self.ainvoke(*args, **kwargs)

    def _parse_provider_response(self, response: Any, **kwargs: Any) -> ModelResponse:
        return ModelResponse(content=str(response))

    def _parse_provider_response_delta(self, response: Any) -> ModelResponse:
        return ModelResponse(content=str(response))


def _stable_worker(args: argparse.Namespace) -> None:
    from agno.agent import Agent
    from agno.db.sqlite import SqliteDb

    agent = Agent(
        id="recovery-probe",
        model=BlockingModel(id="blocking-model", marker=args.marker),
        db=SqliteDb(db_file=args.sqlite),
        session_id=args.session_id,
        user_id="recovery-user",
    )
    agent.run("probe interrupted model boundary", run_id=args.run_id)


def _stable_probe() -> dict[str, Any]:
    from agno.db.sqlite import SqliteDb

    with tempfile.TemporaryDirectory(prefix="agnoclaw-agno2-kill-") as directory:
        root = Path(directory)
        sqlite_path = root / "stable.db"
        marker = root / "model-entered"
        session_id = f"session-{uuid4().hex}"
        run_id = f"run-{uuid4().hex}"
        child = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "_stable-worker",
                "--sqlite",
                str(sqlite_path),
                "--marker",
                str(marker),
                "--session-id",
                session_id,
                "--run-id",
                run_id,
            ]
        )
        try:
            _wait_for(marker)
            child.kill()
            return_code = child.wait(timeout=10)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)

        db = SqliteDb(db_file=str(sqlite_path))
        session = db.get_session(session_id=session_id, user_id="recovery-user")
        runs = list(getattr(session, "runs", None) or []) if session is not None else []
        matched = next((run for run in runs if getattr(run, "run_id", None) == run_id), None)
        status = getattr(getattr(matched, "status", None), "value", None)
        return {
            "agno_line": "stable-2.x",
            "boundary": "model request entered; no response observed",
            "real_process_killed": return_code < 0,
            "session_persisted": session is not None,
            "run_persisted": matched is not None,
            "persisted_status": status,
            "certified_resume_boundary": False,
            "finding": (
                "session/run persistence is an observation, not a provider receipt or "
                "effect-settlement checkpoint; continue_run is a pause/fork/regenerate API"
            ),
        }


def _job(job_id: str, *, now: int) -> dict[str, Any]:
    return {
        "id": job_id,
        "component_type": "agent",
        "job_type": "run",
        "deployment_id": None,
        "component_id": "recovery-probe",
        "session_id": f"session-{job_id}",
        "user_id": "recovery-user",
        "payload": {"input": "probe"},
        "status": "queued",
        "attempt": 0,
        "max_attempts": 2,
        "idempotency_key": f"idem-{job_id}",
        "available_at": now,
        "locked_by": None,
        "locked_at": None,
        "error": None,
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
    }


def _preview_effect_worker(args: argparse.Namespace) -> None:
    from agno.db.postgres import PostgresDb

    # The job queue is an Agno 3 preview API. Stable Agno type information intentionally
    # does not advertise it, while this quarantined probe runs only in the preview lane.
    preview_db: Any = PostgresDb
    db = preview_db(db_url=args.db_url, job_table=args.job_table)
    claimed = db.claim_job(args.worker_id, args.lock_grace)
    if claimed is None or claimed["id"] != args.job_id:
        raise RuntimeError(f"worker could not claim expected job: {claimed}")
    if args.phase == "after-effect":
        with Path(args.effect_log).open("a", encoding="utf-8") as handle:
            handle.write(f"{args.job_id}:{args.worker_id}:attempt-{claimed['attempt']}\n")
            handle.flush()
            os.fsync(handle.fileno())
    Path(args.ready).write_text(json.dumps(claimed), encoding="utf-8")
    time.sleep(300)


def _run_preview_boundary(
    *, db: Any, db_url: str, job_table: str, phase: str, root: Path, lock_grace: int
) -> dict[str, Any]:
    now = int(time.time())
    job_id = f"probe-{phase}-{uuid4().hex}"
    accepted = db.enqueue_job(_job(job_id, now=now), max_depth=10)
    if not accepted.get("accepted"):
        raise RuntimeError(f"job was not accepted: {accepted}")

    ready = root / f"{job_id}.ready"
    effect_log = root / f"{job_id}.effects"
    worker_one = f"worker-one-{uuid4().hex[:8]}"
    child = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "_preview-effect-worker",
            "--db-url",
            db_url,
            "--job-table",
            job_table,
            "--job-id",
            job_id,
            "--worker-id",
            worker_one,
            "--phase",
            phase,
            "--ready",
            str(ready),
            "--effect-log",
            str(effect_log),
            "--lock-grace",
            str(lock_grace),
        ]
    )
    try:
        _wait_for(ready)
        first_claim = json.loads(ready.read_text(encoding="utf-8"))
        child.kill()
        return_code = child.wait(timeout=10)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)

    time.sleep(lock_grace + 1)
    worker_two = f"worker-two-{uuid4().hex[:8]}"
    second_claim = db.claim_job(worker_two, lock_grace)
    if second_claim is None or second_claim["id"] != job_id:
        raise RuntimeError(f"job was not reclaimed: {second_claim}")

    with effect_log.open("a", encoding="utf-8") as handle:
        handle.write(f"{job_id}:{worker_two}:attempt-{second_claim['attempt']}\n")
        handle.flush()
        os.fsync(handle.fileno())

    stale_completion_applied = db.complete_job(
        job_id, worker_one, first_claim["attempt"], "completed"
    )
    current_completion_applied = db.complete_job(
        job_id, worker_two, second_claim["attempt"], "completed"
    )
    final = db.get_job(job_id)
    effects = effect_log.read_text(encoding="utf-8").splitlines()
    expected_effects = 1 if phase == "before-effect" else 2
    assert len(effects) == expected_effects, effects
    assert stale_completion_applied is False
    assert current_completion_applied is True
    assert final is not None and final["status"] == "completed"
    return {
        "boundary": phase,
        "real_process_killed": return_code < 0,
        "first_attempt": first_claim["attempt"],
        "reclaimed_attempt": second_claim["attempt"],
        "external_effect_count": len(effects),
        "stale_completion_fenced": not stale_completion_applied,
        "current_completion_applied": current_completion_applied,
        "final_ticket_status": final["status"],
    }


def _preview_probe(db_url: str) -> dict[str, Any]:
    from agno.db.postgres import PostgresDb

    lock_grace = 3
    # Agno suffixes partial-index names to this table name; keep enough headroom
    # under Postgres' 63-character identifier limit.
    job_table = f"acr_{uuid4().hex[:8]}"
    preview_db: Any = PostgresDb
    db = preview_db(db_url=db_url, job_table=job_table)
    with tempfile.TemporaryDirectory(prefix="agnoclaw-agno3-kill-") as directory:
        root = Path(directory)
        before = _run_preview_boundary(
            db=db,
            db_url=db_url,
            job_table=job_table,
            phase="before-effect",
            root=root,
            lock_grace=lock_grace,
        )
        after = _run_preview_boundary(
            db=db,
            db_url=db_url,
            job_table=job_table,
            phase="after-effect",
            root=root,
            lock_grace=lock_grace,
        )
    return {
        "agno_line": "preview-3",
        "queue_store": "PostgresDb",
        "before_effect": before,
        "after_effect": after,
        "certified_queue_reclaim_and_fencing": True,
        "certified_exactly_once_external_effects": False,
        "finding": (
            "queue attempts and terminal writes are fenced, but a process death after an "
            "external effect and before settlement retries the run and duplicates the effect"
        ),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("stable")

    preview = subparsers.add_parser("preview")
    preview.add_argument("--db-url", required=True)

    stable_worker = subparsers.add_parser("_stable-worker")
    stable_worker.add_argument("--sqlite", required=True)
    stable_worker.add_argument("--marker", required=True)
    stable_worker.add_argument("--session-id", required=True)
    stable_worker.add_argument("--run-id", required=True)

    effect_worker = subparsers.add_parser("_preview-effect-worker")
    effect_worker.add_argument("--db-url", required=True)
    effect_worker.add_argument("--job-table", required=True)
    effect_worker.add_argument("--job-id", required=True)
    effect_worker.add_argument("--worker-id", required=True)
    effect_worker.add_argument("--phase", choices=("before-effect", "after-effect"), required=True)
    effect_worker.add_argument("--ready", required=True)
    effect_worker.add_argument("--effect-log", required=True)
    effect_worker.add_argument("--lock-grace", type=int, required=True)
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.command == "_stable-worker":
        _stable_worker(args)
        return
    if args.command == "_preview-effect-worker":
        _preview_effect_worker(args)
        return
    result = _stable_probe() if args.command == "stable" else _preview_probe(args.db_url)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
