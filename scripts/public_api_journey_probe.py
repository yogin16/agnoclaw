#!/usr/bin/env python3
"""Exercise the public short-run, durable, learning, and migration journeys offline.

The probe uses a deterministic host model and disposable local stores. It performs no
provider calls, imports no private agnoclaw modules, and prints one content-free JSON
record. The same file is intended to run first from source and later unchanged in an
isolated environment containing only the release wheel.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import tempfile
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from typing import Any

from agno.models.base import Model
from agno.models.response import ModelResponse

from agnoclaw import (
    AgentHarness,
    AgnoModelFactory,
    CandidateAuthor,
    CandidateEvaluation,
    CandidateRisk,
    CandidateState,
    EvaluationVerdict,
    ExecutionContext,
    HarnessConfig,
    LearningCandidate,
    LearningProfile,
    LearningTarget,
    LegacyLearningScopeMapping,
    LegacyScopeAction,
    LocalArtifactStore,
    PromotionActor,
    SQLiteLearningLedger,
    SQLiteRuntimeStore,
    apply_migration_012,
    create_migration_012_plan,
    cutover_migration_012,
    rollback_migration_012,
    verify_migration_012,
)

_PROTOCOL_VERSION = "1.0"
_MODEL_DIGEST = "sha256:" + "a" * 64


class JourneyConfigurationError(RuntimeError):
    """The requested path would not be a disposable clean-room journey."""


@dataclass
class JourneyModel(Model):
    """Deterministic provider-free model with explicit transport cleanup evidence."""

    invocation_counter: list[int] | None = None
    close_counter: list[int] | None = None

    def _response(self) -> ModelResponse:
        if self.invocation_counter is not None:
            self.invocation_counter[0] += 1
        return ModelResponse(content="public-api-journey-ready")

    def invoke(self, *args: Any, **kwargs: Any) -> ModelResponse:
        del args, kwargs
        return self._response()

    async def ainvoke(self, *args: Any, **kwargs: Any) -> ModelResponse:
        return self.invoke(*args, **kwargs)

    def invoke_stream(self, *args: Any, **kwargs: Any) -> Iterator[ModelResponse]:
        yield self.invoke(*args, **kwargs)

    async def ainvoke_stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[ModelResponse]:
        yield self.invoke(*args, **kwargs)

    def _parse_provider_response(self, response: Any, **kwargs: Any) -> ModelResponse:
        del kwargs
        return ModelResponse(content=str(response))

    def _parse_provider_response_delta(self, response: Any) -> ModelResponse:
        return ModelResponse(content=str(response))

    def close(self) -> None:
        if self.close_counter is not None:
            self.close_counter[0] += 1


class JourneyPromotionAdapter:
    """Deterministic reversible promotion seam used by the public learning journey."""

    def __init__(self) -> None:
        self.applied: list[str] = []
        self.rolled_back: list[str] = []

    async def apply(
        self,
        candidate: LearningCandidate,
        content: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> str:
        del content, idempotency_key
        self.applied.append(candidate.candidate_id)
        return f"journey:{candidate.candidate_id}"

    async def rollback(
        self,
        candidate: LearningCandidate,
        content: dict[str, Any],
        *,
        target_reference: str,
        idempotency_key: str,
    ) -> None:
        del content, target_reference, idempotency_key
        self.rolled_back.append(candidate.candidate_id)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        help="Use an existing empty directory instead of an auto-cleaned temporary one.",
    )
    return parser


def _require_empty_root(path: Path) -> Path:
    resolved = path.expanduser().resolve(strict=False)
    resolved.mkdir(parents=True, exist_ok=True)
    if any(resolved.iterdir()):
        raise JourneyConfigurationError("--root must reference an empty directory")
    return resolved


def _factory(invocations: list[int], closes: list[int]) -> AgnoModelFactory:
    return AgnoModelFactory(
        model_id="public-api-journey",
        provider="deterministic",
        implementation_digest=_MODEL_DIGEST,
        factory=lambda: JourneyModel(
            id="public-api-journey",
            provider="deterministic",
            invocation_counter=invocations,
            close_counter=closes,
        ),
    )


def _config(root: Path, *, durable: bool) -> HarnessConfig:
    settings = {
        "workspace_dir": str(root / "workspace"),
        "network_enabled": False,
        "enable_plugins": False,
        "storage": {"sqlite_path": str(root / "agno.db")},
    }
    return HarnessConfig.durable(**settings) if durable else HarnessConfig.quick(**settings)


async def _quick_journey(root: Path) -> dict[str, Any]:
    invocations = [0]
    closes = [0]
    harness = AgentHarness(
        _factory(invocations, closes),
        config=_config(root, durable=False),
        include_default_tools=False,
    )
    try:
        result = await harness.arun("run the deterministic short journey")
        if result.content != "public-api-journey-ready":
            raise AssertionError("quick result did not match the deterministic model")
        manifest = harness.runtime_manifest()
        if manifest.profile != "quick":
            raise AssertionError("quick journey did not compile the quick profile")
    finally:
        await harness.aclose()
    if (invocations[0], closes[0]) != (1, 2):
        raise AssertionError("quick factory ownership did not close base and run models")
    return {
        "profile": "quick",
        "model_invocations": invocations[0],
        "owned_models_closed": closes[0],
        "terminal": True,
    }


async def _durable_learning_journey(root: Path) -> dict[str, Any]:
    invocations = [0]
    closes = [0]
    runtime_path = root / "runtime.db"
    learning_path = root / "learning.db"
    artifacts_path = root / "artifacts"
    context = ExecutionContext.create(
        tenant_id="clean-room-tenant",
        user_id="clean-room-user",
        session_id="clean-room-session",
        workspace_id="clean-room-workspace",
        roles=("operator",),
        scopes=("agents.run",),
        request_id="clean-room-request",
    )
    learning = LearningProfile.institutional(
        namespace="clean-room-learning",
        knowledge=SimpleNamespace(vector_db=object()),
    )
    adapter = JourneyPromotionAdapter()
    runtime = SQLiteRuntimeStore(runtime_path)
    harness = AgentHarness(
        _factory(invocations, closes),
        config=_config(root, durable=True),
        include_default_tools=False,
        runtime_store=runtime,
        artifact_store=LocalArtifactStore(artifacts_path),
    )
    try:
        first = await harness.start(
            "run the first durable journey",
            context=context,
            idempotency_key="clean-room:first",
        )
        first_result = await first.wait()
        session = harness.session(context=context)
        follow_up = await session.start(
            "continue the durable journey",
            idempotency_key="clean-room:follow-up",
        )
        follow_up_result = await follow_up.wait()
        if {
            first_result.content,
            follow_up_result.content,
        } != {"public-api-journey-ready"}:
            raise AssertionError("durable result did not match the deterministic model")
        first_run_id = first.run_id
        follow_up_run_id = follow_up.run_id
    finally:
        await harness.aclose()
        runtime.close()

    learning_runtime = SQLiteRuntimeStore(runtime_path)
    ledger = SQLiteLearningLedger(learning_path)
    learning_harness = AgentHarness(
        _factory(invocations, closes),
        config=_config(root, durable=True),
        include_default_tools=False,
        runtime_store=learning_runtime,
        artifact_store=LocalArtifactStore(artifacts_path),
        learning=learning,
        learning_ledger=ledger,
        learning_promotion_adapter=adapter,
    )
    try:
        candidate = await learning_harness.capture_learning_candidate(
            context=context,
            target=LearningTarget.LEARNED_KNOWLEDGE,
            content={"title": "Safe retry", "learning": "Retry only verified safe reads."},
            source_run_ids=(first_run_id,),
            evidence_artifact_ids=("clean-room-source-evidence",),
            confidence=0.95,
            risk=CandidateRisk.LOW,
            created_by=CandidateAuthor.AGENT,
            mechanism_version="clean-room-reflector:v1",
            candidate_id="clean-room-candidate",
        )
        if candidate.state is not CandidateState.CAPTURED:
            raise AssertionError("learning candidate was not captured")
        qualified = await learning_harness.record_learning_candidate_evaluation(
            CandidateEvaluation(
                evaluation_id="clean-room-evaluation",
                candidate_id="clean-room-candidate",
                verdict=EvaluationVerdict.QUALIFIED,
                evaluator_digest="sha256:" + "b" * 64,
                evidence_artifact_ids=("clean-room-held-out-evidence",),
                safety_passed=True,
                evaluated_by=PromotionActor.OPERATOR,
                metrics={"held_out": 1.0},
                control_metrics={"held_out": 0.0},
            ),
            context=context,
            mutation_id="clean-room:evaluate",
        )
        promoted = await learning_harness.promote_learning_candidate(
            "clean-room-candidate",
            context=context,
            actor=PromotionActor.OPERATOR,
            mutation_id="clean-room:promote",
        )
        if qualified.state is not CandidateState.QUALIFIED:
            raise AssertionError("learning candidate was not qualified")
        if promoted.state is not CandidateState.PROMOTED or adapter.applied != [
            "clean-room-candidate"
        ]:
            raise AssertionError("learning candidate was not promoted exactly once")
    finally:
        await learning_harness.aclose()
        ledger.close()
        learning_runtime.close()

    reopened_runtime = SQLiteRuntimeStore(runtime_path)
    reopened_ledger = SQLiteLearningLedger(learning_path)
    reopened = AgentHarness(
        _factory(invocations, closes),
        config=_config(root, durable=True),
        include_default_tools=False,
        runtime_store=reopened_runtime,
        artifact_store=LocalArtifactStore(artifacts_path),
        learning=learning,
        learning_ledger=reopened_ledger,
        learning_promotion_adapter=JourneyPromotionAdapter(),
    )
    try:
        first_handle = reopened.get_run(first_run_id, context=context)
        follow_up_handle = reopened.get_run(follow_up_run_id, context=context)
        reopened_candidate = await reopened.get_learning_candidate(
            "clean-room-candidate",
            context=context,
        )
        first_snapshot = await first_handle.status()
        follow_up_snapshot = await follow_up_handle.status()
        if (
            first_snapshot.state.value != "completed"
            or follow_up_snapshot.state.value != "completed"
        ):
            raise AssertionError("durable run state did not survive reopen")
        if reopened_candidate.state is not CandidateState.PROMOTED:
            raise AssertionError("promoted learning state did not survive reopen")
    finally:
        await reopened.aclose()
        reopened_ledger.close()
        reopened_runtime.close()

    if invocations[0] != 2:
        raise AssertionError("durable journey made an unexpected model call")
    if closes[0] != 5:
        raise AssertionError("durable factories did not close all base/run models")
    return {
        "profile": "durable",
        "logical_runs": 2,
        "model_invocations": invocations[0],
        "owned_models_closed": closes[0],
        "reopened_completed_runs": 2,
        "learning_state": "promoted",
        "learning_effects": len(adapter.applied),
    }


def _create_legacy_learning_source(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE agno_learnings (learning_id TEXT PRIMARY KEY, "
            "learning_type TEXT NOT NULL, namespace TEXT, user_id TEXT, "
            "agent_id TEXT, team_id TEXT, workflow_id TEXT, session_id TEXT, "
            "entity_id TEXT, entity_type TEXT, content TEXT, metadata TEXT, "
            "created_at INTEGER, updated_at INTEGER)"
        )
        connection.execute(
            "INSERT INTO agno_learnings(learning_id,learning_type,namespace,user_id,content) "
            "VALUES(?,?,?,?,?)",
            ("legacy-profile", "user_profile", "user", "legacy-user", '{"name":"Ada"}'),
        )


def _migration_journey(root: Path) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    source = root / "legacy-learning.db"
    target = root / "migrated-learning.db"
    _create_legacy_learning_source(source)
    plan = create_migration_012_plan(
        learning_sqlite_path=source,
        target_learning_db=target,
        target_tenant_id="clean-room-tenant",
        target_agent_id="clean-room-agent",
        scope_mappings=(
            LegacyLearningScopeMapping(
                source_namespace="user",
                learning_type="user_profile",
                action=LegacyScopeAction.MAP,
                target_tenant_id="clean-room-tenant",
                target_namespace="user:clean-room-user",
            ),
        ),
        old_writer_fence_plan="clean-room-writers-stopped:v1",
    )
    state = root / "migration-state"
    applied = apply_migration_012(
        plan,
        state_dir=state,
        confirm_plan_digest=plan.plan_digest,
        writers_stopped=True,
    )
    verified = verify_migration_012(state_dir=state)
    cutover = cutover_migration_012(
        state_dir=state,
        confirm_migration_id=plan.migration_id,
    )
    rollback = rollback_migration_012(
        state_dir=state,
        confirm_migration_id=plan.migration_id,
        writers_stopped=True,
    )
    phases = [
        applied["phase"],
        verified["phase"],
        cutover["phase"],
        rollback["phase"],
    ]
    expected = ["applied", "verified", "cutover", "rolled_back"]
    if phases != expected or target.exists():
        raise AssertionError("migration lifecycle did not verify and roll back cleanly")
    return {
        "phases": phases,
        "personal_rows": applied["imports"]["learning"]["personal_rows"],
        "rollback_removed_target": True,
    }


async def _run(root: Path) -> dict[str, Any]:
    started = monotonic()
    quick = await _quick_journey(root / "quick")
    durable = await _durable_learning_journey(root / "durable")
    migration = _migration_journey(root / "migration")
    return {
        "schema_version": _PROTOCOL_VERSION,
        "agnoclaw_version": version("agnoclaw"),
        "quick": quick,
        "durable_and_learning": durable,
        "migration": migration,
        "provider_network_calls": 0,
        "network_boundary": "provider_free; OS network denial is a separate installed-wheel gate",
        "production_certification": False,
        "elapsed_seconds": round(monotonic() - started, 3),
        "cleanup": "complete",
    }


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.root is not None:
            root = _require_empty_root(args.root)
            report = asyncio.run(_run(root))
        else:
            with tempfile.TemporaryDirectory(prefix="agnoclaw-public-journey-") as directory:
                report = asyncio.run(_run(Path(directory)))
    except JourneyConfigurationError as exc:
        print(
            json.dumps(
                {
                    "schema_version": _PROTOCOL_VERSION,
                    "error": type(exc).__name__,
                    "message": str(exc),
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
