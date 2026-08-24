"""AgentHarness-facing learning reconciliation composition.

The mixin keeps the saturated main facade small while exposing one ergonomic default:
when no observer is supplied, the same Agno machine factory used for promotion powers
the exact-name observer. Custom promotion backends must still supply their own observer.
"""

from __future__ import annotations

from .compat import AgnoFeature, inspect_agno_compatibility
from .learning import LearningScope
from .learning_candidates import (
    AgnoLearningPromotionAdapter,
    LearningGateway,
    LearningOwner,
    PromotionActor,
    ReconciliationCursor,
    ReconciliationPage,
)
from .learning_reconciliation import (
    AgnoLearnedKnowledgeReconciliationObserver,
    LearningReconciliationCoordinator,
    LearningReconciliationObserver,
    ReconciliationBatchOutcome,
)
from .learning_reconciliation_worker import (
    LearningReconciliationWorker,
    LearningReconciliationWorkerConfig,
)
from .runtime.context import ExecutionContext
from .runtime.errors import HarnessError


class _LearningReconciliationMixin:
    """Typed composition seam implemented by ``AgentHarness`` internals."""

    def _require_learning_gateway(self, *, write: bool = False) -> LearningGateway:
        raise NotImplementedError

    def _resolve_candidate_scope(
        self,
        context: ExecutionContext,
        *,
        learning_consent: bool,
    ) -> tuple[ExecutionContext, LearningScope, LearningOwner]:
        raise NotImplementedError

    @staticmethod
    def _build_reconciliation_coordinator(
        gateway: LearningGateway,
        observer: LearningReconciliationObserver | None,
        *,
        reconciler_digest: str,
        reconciled_by: PromotionActor,
        max_concurrency: int,
    ) -> LearningReconciliationCoordinator:
        resolved_observer = observer
        if resolved_observer is None:
            inspect_agno_compatibility().require(AgnoFeature.LEARNING_EXACT_NAME_INSPECTION)
            adapter = gateway.promotion_adapter
            if not isinstance(adapter, AgnoLearningPromotionAdapter):
                raise HarnessError(
                    code="LEARNING_RECONCILIATION_OBSERVER_REQUIRED",
                    category="learning",
                    message=(
                        "A custom promotion backend requires a matching exact-state observer."
                    ),
                    retryable=False,
                )
            resolved_observer = AgnoLearnedKnowledgeReconciliationObserver(
                adapter.machine_factory,
                gateway.artifact_store,
                observer_identity_digest=reconciler_digest,
            )
        return LearningReconciliationCoordinator(
            gateway,
            resolved_observer,
            reconciler_digest=reconciler_digest,
            reconciled_by=reconciled_by,
            max_concurrency=max_concurrency,
        )

    async def scan_learning_reconciliation_required(
        self,
        *,
        context: ExecutionContext,
        limit: int = 100,
        cursor: ReconciliationCursor | None = None,
        learning_consent: bool = False,
    ) -> ReconciliationPage:
        """Page unknown learning effects after exact-scope reauthorization."""
        gateway = self._require_learning_gateway()
        _, _, owner = self._resolve_candidate_scope(
            context,
            learning_consent=learning_consent,
        )
        return await gateway.scan_reconciliation_required(
            owner=owner,
            limit=limit,
            cursor=cursor,
        )

    async def observe_learning_reconciliation_page(
        self,
        observer: LearningReconciliationObserver | None = None,
        *,
        context: ExecutionContext,
        reconciler_digest: str,
        reconciled_by: PromotionActor = PromotionActor.HOST,
        limit: int = 100,
        cursor: ReconciliationCursor | None = None,
        max_concurrency: int = 4,
        learning_consent: bool = False,
    ) -> ReconciliationBatchOutcome:
        """Observe and evidence-settle one bounded page; never replay the effect.

        Omitting ``observer`` selects the exact Agno Learned Knowledge observer only
        when the gateway uses agnoclaw's first-party promotion adapter.
        """
        gateway = self._require_learning_gateway(write=True)
        _, _, owner = self._resolve_candidate_scope(
            context,
            learning_consent=learning_consent,
        )
        coordinator = self._build_reconciliation_coordinator(
            gateway,
            observer,
            reconciler_digest=reconciler_digest,
            reconciled_by=reconciled_by,
            max_concurrency=max_concurrency,
        )
        return await coordinator.run_page(
            owner=owner,
            limit=limit,
            cursor=cursor,
        )

    def build_learning_reconciliation_worker(
        self,
        observer: LearningReconciliationObserver | None = None,
        *,
        context: ExecutionContext,
        reconciler_digest: str,
        config: LearningReconciliationWorkerConfig,
        reconciled_by: PromotionActor = PromotionActor.HOST,
        learning_consent: bool = False,
    ) -> LearningReconciliationWorker:
        """Build one owner-scoped durable worker from public harness inputs."""
        gateway = self._require_learning_gateway(write=True)
        _, _, owner = self._resolve_candidate_scope(
            context,
            learning_consent=learning_consent,
        )
        coordinator = self._build_reconciliation_coordinator(
            gateway,
            observer,
            reconciler_digest=reconciler_digest,
            reconciled_by=reconciled_by,
            max_concurrency=config.max_concurrency,
        )
        return LearningReconciliationWorker(
            coordinator,
            owner=owner,
            config=config,
        )


__all__ = ["_LearningReconciliationMixin"]
