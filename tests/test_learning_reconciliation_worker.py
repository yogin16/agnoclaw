"""Durable learning-reconciliation worker leases, fencing, and recovery."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agnoclaw import (
    CandidateAuthor,
    CandidateEvaluation,
    CandidateRisk,
    EvaluationVerdict,
    LearningGateway,
    LearningLedgerConnectionLostError,
    LearningOwner,
    LearningProfile,
    LearningReconciliationCoordinator,
    LearningReconciliationWorker,
    LearningReconciliationWorkerConfig,
    LearningReconciliationWorkerLeaseError,
    LearningScope,
    LearningTarget,
    LocalArtifactStore,
    PromotionActor,
    ReconciliationCursor,
    ReconciliationObservation,
    ReconciliationVerdict,
    SQLiteLearningLedger,
)
from agnoclaw.learning_reconciliation import RECONCILIATION_EVIDENCE_PURPOSE
from agnoclaw.runtime import ExecutionContext


def _owner() -> LearningOwner:
    return LearningOwner("tenant-a", "learning:tenant-a:user-a")


def test_sqlite_worker_lease_is_exclusive_fenced_safe_and_restart_durable(tmp_path) -> None:
    path = tmp_path / "learning.db"
    owner = _owner()
    ledger = SQLiteLearningLedger(path)
    first = ledger.claim_reconciliation_worker(
        owner=owner,
        worker_id="worker-a",
        lease_seconds=30,
    )
    assert first is not None
    assert ledger.claim_reconciliation_worker(
        owner=owner,
        worker_id="worker-b",
        lease_seconds=30,
    ) is None
    assert first.lease_token not in repr(first)
    assert "lease_token" not in first.to_dict()

    cursor = ReconciliationCursor(
        updated_at="2026-08-14T00:00:00+00:00",
        candidate_id="lc-cursor",
        owner_digest=owner.digest,
    )
    checkpoint = ledger.checkpoint_reconciliation_worker(
        first,
        cursor=cursor,
        lease_seconds=30,
    )
    assert checkpoint.cursor == cursor
    assert ledger.release_reconciliation_worker(checkpoint) is True
    with pytest.raises(LearningReconciliationWorkerLeaseError):
        ledger.checkpoint_reconciliation_worker(
            first,
            cursor=None,
            lease_seconds=30,
        )
    ledger.close()

    reopened = SQLiteLearningLedger(path)
    takeover = reopened.claim_reconciliation_worker(
        owner=owner,
        worker_id="worker-b",
        lease_seconds=30,
    )
    assert takeover is not None
    assert takeover.fence == first.fence + 1
    assert takeover.cursor == cursor
    assert reopened.release_reconciliation_worker(first) is False
    assert reopened.release_reconciliation_worker(takeover) is True
    reopened.close()


@pytest.mark.asyncio
async def test_sqlite_expired_worker_lease_can_be_taken_over_but_not_revived(tmp_path) -> None:
    ledger = SQLiteLearningLedger(tmp_path / "learning.db")
    owner = _owner()
    expired = ledger.claim_reconciliation_worker(
        owner=owner,
        worker_id="worker-expired",
        lease_seconds=1,
    )
    assert expired is not None
    await asyncio.sleep(1.05)
    takeover = ledger.claim_reconciliation_worker(
        owner=owner,
        worker_id="worker-takeover",
        lease_seconds=30,
    )
    assert takeover is not None
    assert takeover.fence == expired.fence + 1
    with pytest.raises(LearningReconciliationWorkerLeaseError):
        ledger.checkpoint_reconciliation_worker(
            expired,
            cursor=None,
            lease_seconds=30,
        )
    assert ledger.release_reconciliation_worker(expired) is False
    assert ledger.release_reconciliation_worker(takeover) is True
    ledger.close()


async def _unknown_candidates(tmp_path, *, count: int):
    policy = LearningProfile.institutional(
        namespace="research",
        knowledge=SimpleNamespace(vector_db=object()),
    )
    context = ExecutionContext.create(
        tenant_id="tenant-worker",
        user_id="user-worker",
        session_id="session-worker",
        workspace_id="workspace-worker",
    )
    scope = LearningScope.resolve(policy, context, agent_id="worker-agent")
    ledger = SQLiteLearningLedger(tmp_path / "learning.db")
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    gateway = LearningGateway(ledger, artifacts)
    records = []
    for index in range(count):
        candidate_id = f"lc-worker-{index}"
        captured = await gateway.capture(
            policy=policy,
            scope=scope,
            target=LearningTarget.LEARNED_KNOWLEDGE,
            content={"title": f"Rule {index}", "learning": f"Bounded rule {index}."},
            source_run_ids=(f"run-{index}",),
            evidence_artifact_ids=(f"artifact-source-{index}",),
            confidence=0.9,
            risk=CandidateRisk.MEDIUM,
            created_by=CandidateAuthor.RULE,
            mechanism_version="worker-test:v1",
            candidate_id=candidate_id,
        )
        qualified = ledger.record_evaluation(
            CandidateEvaluation(
                evaluation_id=f"eval-worker-{index}",
                candidate_id=candidate_id,
                verdict=EvaluationVerdict.QUALIFIED,
                evaluator_digest="sha256:" + "e" * 64,
                evidence_artifact_ids=(f"artifact-eval-{index}",),
                safety_passed=True,
                evaluated_by=PromotionActor.HOST,
            ),
            owner=captured.candidate.owner,
            expected_revision=captured.revision,
            mutation_id=f"evaluate-worker-{index}",
        )
        promoting = ledger.begin_promotion(
            candidate_id,
            owner=captured.candidate.owner,
            expected_revision=qualified.revision,
            mutation_id=f"promote-worker-{index}:begin",
            promotion_id=f"promotion-worker-{index}",
            actor=PromotionActor.HOST,
        )
        records.append(
            ledger.settle_promotion(
                candidate_id,
                owner=captured.candidate.owner,
                expected_revision=promoting.revision,
                mutation_id=f"promote-worker-{index}:unknown",
                succeeded=False,
                target_reference=None,
            )
        )
    return ledger, artifacts, gateway, tuple(records)


class _AbsentObserver:
    def __init__(self, artifacts: LocalArtifactStore, *, delay: float = 0) -> None:
        self.artifacts = artifacts
        self.delay = delay
        self.calls = 0

    async def observe(self, request, content):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        assert content["learning"].startswith("Bounded rule")
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
    gateway: LearningGateway,
    owner: LearningOwner,
    observer: _AbsentObserver,
    *,
    worker_id: str,
    lease_seconds: int = 30,
) -> LearningReconciliationWorker:
    coordinator = LearningReconciliationCoordinator(
        gateway,
        observer,
        reconciler_digest="sha256:" + "a" * 64,
    )
    return LearningReconciliationWorker(
        coordinator,
        owner=owner,
        config=LearningReconciliationWorkerConfig(
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            poll_interval_seconds=0.01,
            page_limit=1,
            heartbeat_interval_seconds=(0.2 if lease_seconds == 1 else None),
        ),
    )


@pytest.mark.asyncio
async def test_worker_cursor_resumes_next_page_after_ledger_restart(tmp_path) -> None:
    ledger, artifacts, gateway, records = await _unknown_candidates(tmp_path, count=2)
    owner = records[0].candidate.owner
    first = _worker(
        gateway,
        owner,
        _AbsentObserver(artifacts),
        worker_id="worker-before-restart",
    )
    first_stats = await first.run_once()
    assert set(first_stats.to_dict()) == {
        "owner_digest",
        "worker_id",
        "claims",
        "pages",
        "items",
        "reconciled",
        "deferred",
        "stale",
        "rejected",
        "failed",
        "lease_losses",
        "store_failures",
    }
    assert (first_stats.claims, first_stats.pages, first_stats.reconciled) == (1, 1, 1)
    ledger.close()

    reopened = SQLiteLearningLedger(tmp_path / "learning.db")
    reopened_gateway = LearningGateway(reopened, artifacts)
    second = _worker(
        reopened_gateway,
        owner,
        _AbsentObserver(artifacts),
        worker_id="worker-after-restart",
    )
    second_stats = await second.run_once()
    assert (second_stats.claims, second_stats.pages, second_stats.reconciled) == (1, 1, 1)
    assert reopened.scan_reconciliation_required(owner=owner).items == ()
    reopened.close()


@pytest.mark.asyncio
async def test_worker_heartbeats_during_slow_observation(tmp_path) -> None:
    ledger, artifacts, gateway, records = await _unknown_candidates(tmp_path, count=1)
    owner = records[0].candidate.owner
    observer = _AbsentObserver(artifacts, delay=1.25)
    worker = _worker(
        gateway,
        owner,
        observer,
        worker_id="worker-slow",
        lease_seconds=1,
    )

    task = asyncio.create_task(worker.run_once())
    await asyncio.sleep(1.05)
    competing = ledger.claim_reconciliation_worker(
        owner=owner,
        worker_id="worker-competing",
        lease_seconds=30,
    )
    assert competing is None
    stats = await task
    assert (stats.reconciled, stats.lease_losses, observer.calls) == (1, 0, 1)
    ledger.close()


@pytest.mark.asyncio
async def test_long_running_worker_wraps_sweep_and_stops_cleanly(tmp_path) -> None:
    ledger, artifacts, gateway, records = await _unknown_candidates(tmp_path, count=1)
    owner = records[0].candidate.owner
    worker = _worker(
        gateway,
        owner,
        _AbsentObserver(artifacts),
        worker_id="worker-continuous",
    )
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    for _ in range(100):
        if not ledger.scan_reconciliation_required(owner=owner).items:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("continuous worker did not settle the bounded page")
    stop.set()
    stats = await asyncio.wait_for(task, timeout=1)
    assert stats.claims >= 1
    assert (stats.items, stats.reconciled, stats.lease_losses) == (1, 1, 0)
    ledger.close()


@pytest.mark.asyncio
async def test_long_running_worker_recovers_from_retryable_store_failure(
    tmp_path,
    monkeypatch,
) -> None:
    ledger, artifacts, gateway, records = await _unknown_candidates(tmp_path, count=1)
    owner = records[0].candidate.owner
    worker = _worker(
        gateway,
        owner,
        _AbsentObserver(artifacts),
        worker_id="worker-store-recovery",
    )
    original_claim = ledger.claim_reconciliation_worker
    attempts = 0

    def fail_once(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise LearningLedgerConnectionLostError()
        return original_claim(**kwargs)

    monkeypatch.setattr(ledger, "claim_reconciliation_worker", fail_once)
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    for _ in range(100):
        if not ledger.scan_reconciliation_required(owner=owner).items:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("worker did not recover after the retryable store failure")
    stop.set()
    stats = await asyncio.wait_for(task, timeout=1)
    assert stats.store_failures == 1
    assert stats.reconciled == 1
    ledger.close()


@pytest.mark.asyncio
async def test_worker_cancels_slow_page_and_reports_lease_loss(tmp_path, monkeypatch) -> None:
    ledger, artifacts, gateway, records = await _unknown_candidates(tmp_path, count=1)
    owner = records[0].candidate.owner
    observer = _AbsentObserver(artifacts, delay=1)
    worker = _worker(
        gateway,
        owner,
        observer,
        worker_id="worker-lease-loss",
        lease_seconds=1,
    )

    def lose_lease(*_args, **_kwargs):
        raise LearningReconciliationWorkerLeaseError()

    monkeypatch.setattr(ledger, "checkpoint_reconciliation_worker", lose_lease)
    stats = await worker.run_once()
    assert (stats.claims, stats.pages, stats.items, stats.lease_losses) == (1, 0, 0, 1)
    assert len(ledger.scan_reconciliation_required(owner=owner).items) == 1
    ledger.close()


def test_worker_config_rejects_unsafe_heartbeat_and_bounds() -> None:
    with pytest.raises(ValueError, match="shorter than the lease"):
        LearningReconciliationWorkerConfig(
            worker_id="worker",
            lease_seconds=1,
            heartbeat_interval_seconds=1,
        )
    with pytest.raises(ValueError, match="page_limit"):
        LearningReconciliationWorkerConfig(worker_id="worker", page_limit=0)
    with pytest.raises(ValueError, match="poll_interval_seconds"):
        LearningReconciliationWorkerConfig(worker_id="worker", poll_interval_seconds=True)
    with pytest.raises(ValueError, match="heartbeat_interval_seconds"):
        LearningReconciliationWorkerConfig(
            worker_id="worker",
            heartbeat_interval_seconds=True,
        )
