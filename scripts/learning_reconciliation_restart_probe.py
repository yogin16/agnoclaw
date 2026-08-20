#!/usr/bin/env python3
"""Kill and reclaim a leased learning-reconciliation worker in real processes."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn

from agnoclaw import (
    RECONCILIATION_EVIDENCE_PURPOSE,
    CandidateAuthor,
    CandidateEvaluation,
    CandidateRisk,
    CandidateState,
    EvaluationVerdict,
    ExecutionContext,
    LearningGateway,
    LearningOwner,
    LearningProfile,
    LearningReconciliationCoordinator,
    LearningReconciliationWorker,
    LearningReconciliationWorkerConfig,
    LearningScope,
    LearningTarget,
    LocalArtifactStore,
    PromotionActor,
    ReconciliationObservation,
    ReconciliationVerdict,
    SQLiteLearningLedger,
)

_CRASH_EXIT_CODE = 90
_LEASE_SECONDS = 3
_CANDIDATE_ID = "lc-process-reconciliation"
_RECONCILER_DIGEST = "sha256:" + "e" * 64


class ProbeConfigurationError(RuntimeError):
    """The requested command would not perform the destructive child exit."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--allow-process-crash",
        action="store_true",
        help="Required acknowledgement that a disposable child calls os._exit().",
    )
    parser.add_argument("--_child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--_root", type=Path, help=argparse.SUPPRESS)
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    if args._child:
        if args._root is None:
            raise ProbeConfigurationError("child mode requires --_root")
        return
    if args._root is not None:
        raise ProbeConfigurationError("--_root is internal")
    if not args.allow_process_crash:
        raise ProbeConfigurationError("--allow-process-crash is required")


def _ledger_path(root: Path) -> Path:
    return root / "learning.db"


def _artifacts_path(root: Path) -> Path:
    return root / "artifacts"


def _entered_path(root: Path) -> Path:
    return root / "observer-entered"


def _write_durable_marker(path: Path) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, b"entered")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _policy() -> Any:
    return LearningProfile.institutional(
        namespace="process-reconciliation",
        knowledge=SimpleNamespace(vector_db=object()),
    )


def _scope() -> LearningScope:
    return LearningScope.resolve(
        _policy(),
        ExecutionContext.create(
            tenant_id="learning-process-tenant",
            user_id="learning-process-user",
            session_id="learning-process-session",
            workspace_id="learning-process-workspace",
        ),
        agent_id="learning-process-agent",
    )


def _owner() -> LearningOwner:
    scope = _scope()
    return LearningOwner(scope.tenant_id, scope.storage_namespace)


async def _prepare_unknown_candidate(root: Path) -> None:
    ledger = SQLiteLearningLedger(_ledger_path(root))
    artifacts = LocalArtifactStore(_artifacts_path(root))
    gateway = LearningGateway(ledger, artifacts)
    try:
        captured = await gateway.capture(
            policy=_policy(),
            scope=_scope(),
            target=LearningTarget.LEARNED_KNOWLEDGE,
            content={
                "title": "Process reconciliation rule",
                "learning": "Only evidence may settle an ambiguous learning effect.",
            },
            source_run_ids=("run-process-reconciliation",),
            evidence_artifact_ids=("artifact-process-source",),
            confidence=0.95,
            risk=CandidateRisk.MEDIUM,
            created_by=CandidateAuthor.RULE,
            mechanism_version="process-reconciliation:v1",
            candidate_id=_CANDIDATE_ID,
        )
        qualified = ledger.record_evaluation(
            CandidateEvaluation(
                evaluation_id="eval-process-reconciliation",
                candidate_id=_CANDIDATE_ID,
                verdict=EvaluationVerdict.QUALIFIED,
                evaluator_digest="sha256:" + "f" * 64,
                evidence_artifact_ids=("artifact-process-evaluation",),
                safety_passed=True,
                evaluated_by=PromotionActor.HOST,
            ),
            owner=captured.candidate.owner,
            expected_revision=captured.revision,
            mutation_id="evaluate-process-reconciliation",
        )
        promoting = ledger.begin_promotion(
            _CANDIDATE_ID,
            owner=captured.candidate.owner,
            expected_revision=qualified.revision,
            mutation_id="promote-process-reconciliation:begin",
            promotion_id="promotion-process-reconciliation",
            actor=PromotionActor.HOST,
        )
        unknown = ledger.settle_promotion(
            _CANDIDATE_ID,
            owner=captured.candidate.owner,
            expected_revision=promoting.revision,
            mutation_id="promote-process-reconciliation:unknown",
            succeeded=False,
            target_reference=None,
        )
        if unknown.state is not CandidateState.PROMOTION_UNKNOWN:
            raise AssertionError("setup did not create one ambiguous learning effect")
    finally:
        ledger.close()


class _CrashObserver:
    def __init__(self, root: Path) -> None:
        self.root = root

    async def observe(self, request: Any, content: dict[str, Any]) -> NoReturn:
        del request, content
        _write_durable_marker(_entered_path(self.root))
        os._exit(_CRASH_EXIT_CODE)


class _AbsentObserver:
    def __init__(self, artifacts: LocalArtifactStore) -> None:
        self.artifacts = artifacts
        self.calls = 0

    async def observe(
        self,
        request: Any,
        content: dict[str, Any],
    ) -> ReconciliationObservation:
        self.calls += 1
        if content.get("title") != "Process reconciliation rule":
            raise AssertionError("recovery observer received the wrong candidate content")
        evidence = await self.artifacts.stage_json(
            {"exact_candidate_effect_present": False},
            scope=request.record.candidate.content_artifact.scope,
            purpose=RECONCILIATION_EVIDENCE_PURPOSE,
        )
        return ReconciliationObservation(
            candidate_id=request.record.candidate.candidate_id,
            kind=request.kind,
            expected_revision=request.record.revision,
            candidate_digest=request.record.candidate.digest,
            verdict=ReconciliationVerdict.EFFECT_ABSENT,
            evidence_artifacts=(evidence,),
        )


def _worker(
    ledger: SQLiteLearningLedger,
    artifacts: LocalArtifactStore,
    observer: Any,
    *,
    worker_id: str,
    lease_seconds: int,
) -> LearningReconciliationWorker:
    gateway = LearningGateway(ledger, artifacts)
    coordinator = LearningReconciliationCoordinator(
        gateway,
        observer,
        reconciler_digest=_RECONCILER_DIGEST,
    )
    return LearningReconciliationWorker(
        coordinator,
        owner=_owner(),
        config=LearningReconciliationWorkerConfig(
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            poll_interval_seconds=0.01,
            page_limit=1,
            max_concurrency=1,
            heartbeat_interval_seconds=0.5,
        ),
    )


async def _child_run(root: Path) -> NoReturn:
    ledger = SQLiteLearningLedger(_ledger_path(root))
    worker = _worker(
        ledger,
        LocalArtifactStore(_artifacts_path(root)),
        _CrashObserver(root),
        worker_id="worker-crashed",
        lease_seconds=_LEASE_SECONDS,
    )
    await worker.run_once()
    raise RuntimeError("crash observer unexpectedly returned")


def _child(root: Path) -> NoReturn:
    asyncio.run(_child_run(root))
    raise RuntimeError("child unexpectedly returned")


def _spawn_child(root: Path) -> None:
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--_child", "--_root", str(root)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != _CRASH_EXIT_CODE:
        raise AssertionError(
            "learning worker did not exit inside observation: "
            f"returncode={completed.returncode}, stderr={completed.stderr[-500:]!r}"
        )
    if not _entered_path(root).is_file():
        raise AssertionError("child exited without durable observer-entry evidence")


def _lease_row(root: Path) -> tuple[str | None, bool, int, bool]:
    with sqlite3.connect(_ledger_path(root)) as connection:
        row = connection.execute(
            """
            SELECT worker_id, lease_token IS NOT NULL, lease_fence, lease_expires_at > 0
            FROM learning_reconciliation_workers
            WHERE owner_digest = ?
            """,
            (_owner().digest,),
        ).fetchone()
    if row is None:
        raise AssertionError("learning worker lease row is missing")
    return str(row[0]) if row[0] is not None else None, bool(row[1]), int(row[2]), bool(row[3])


def _integrity(root: Path) -> str:
    with sqlite3.connect(_ledger_path(root)) as connection:
        return str(connection.execute("PRAGMA integrity_check").fetchone()[0])


async def _exercise() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="agnoclaw-learning-reconciliation-") as directory:
        root = Path(directory)
        await _prepare_unknown_candidate(root)
        await asyncio.to_thread(_spawn_child, root)

        crashed_worker, token_present, first_fence, active = _lease_row(root)
        if (crashed_worker, token_present, first_fence, active) != (
            "worker-crashed",
            True,
            1,
            True,
        ):
            raise AssertionError("crashed worker did not leave the exact active lease")

        ledger = SQLiteLearningLedger(_ledger_path(root))
        try:
            competing = ledger.claim_reconciliation_worker(
                owner=_owner(),
                worker_id="worker-too-early",
                lease_seconds=30,
            )
            if competing is not None:
                raise AssertionError("a live crashed-worker lease was stolen before expiry")
        finally:
            ledger.close()

        await asyncio.sleep(_LEASE_SECONDS + 0.25)
        reopened = SQLiteLearningLedger(_ledger_path(root))
        artifacts = LocalArtifactStore(_artifacts_path(root))
        observer = _AbsentObserver(artifacts)
        worker = _worker(
            reopened,
            artifacts,
            observer,
            worker_id="worker-recovery",
            lease_seconds=30,
        )
        try:
            stats = await worker.run_once()
            record = await LearningGateway(reopened, artifacts).get(
                _CANDIDATE_ID,
                owner=_owner(),
            )
            reconciliations = reopened.list_reconciliations(
                _CANDIDATE_ID,
                owner=_owner(),
            )
            pending = reopened.scan_reconciliation_required(owner=_owner()).items
        finally:
            reopened.close()

        released_worker, token_after, second_fence, lease_after = _lease_row(root)
        if (released_worker, token_after, second_fence, lease_after) != (
            None,
            False,
            2,
            False,
        ):
            raise AssertionError("replacement worker did not fence and release exactly")
        if (
            stats.claims,
            stats.pages,
            stats.items,
            stats.reconciled,
            stats.failed,
            stats.lease_losses,
            observer.calls,
        ) != (1, 1, 1, 1, 0, 0, 1):
            raise AssertionError("replacement worker counters contradict exact recovery")
        if record.state is not CandidateState.QUALIFIED or pending:
            raise AssertionError("ambiguous candidate did not return to qualified review")
        if len(reconciliations) != 1:
            raise AssertionError("learning reconciliation evidence was duplicated")
        if _integrity(root) != "ok":
            raise AssertionError("learning ledger failed integrity after process death")

        return {
            "status": "passed",
            "scope": "real-process-learning-reconciliation-worker-restart",
            "real_process_crashes": 1,
            "active_lease_steals": 0,
            "lease_fence_before": first_fence,
            "lease_fence_after": second_fence,
            "reconciled_candidates": stats.reconciled,
            "external_observations": observer.calls,
            "promotion_redispatches": 0,
            "duplicate_reconciliations": 0,
            "final_candidate_state": record.state.value,
            "released_recovery_lease": True,
            "database_integrity": True,
            "cleanup": "complete",
        }


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    try:
        _validate_args(args)
    except ProbeConfigurationError as exc:
        parser.error(str(exc))
    if args._child:
        _child(args._root)
    report = asyncio.run(_exercise())
    print(json.dumps(report, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
