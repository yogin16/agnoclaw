"""Bounded-pool PostgreSQL authority for governed learning candidates."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from .learning_candidates import (
    _CANDIDATE_TRANSITIONS,
    LEARNING_LEDGER_SCHEMA_VERSION,
    CandidateAction,
    CandidateConflictError,
    CandidateEvaluation,
    CandidateEvaluationNotFoundError,
    CandidateNotFoundError,
    CandidateReconciliation,
    CandidateReconciliationNotFoundError,
    CandidateRecord,
    CandidateRevisionError,
    CandidateState,
    CandidateTransitionError,
    EvaluationArchiveCursor,
    EvaluationArchivePage,
    EvaluationArchiveQuery,
    EvaluationVerdict,
    LearningApplication,
    LearningApplicationKind,
    LearningApplicationNotFoundError,
    LearningAttributionConflictError,
    LearningCandidate,
    LearningEffectivenessPolicy,
    LearningEffectivenessSummary,
    LearningEvent,
    LearningOutboxItem,
    LearningOutboxLeaseError,
    LearningOutcome,
    LearningOutcomeNotFoundError,
    LearningOwner,
    LearningReconciliationWorkerLease,
    LearningReconciliationWorkerLeaseError,
    PromotionActor,
    ReconciliationCursor,
    ReconciliationCursorScopeError,
    ReconciliationKind,
    ReconciliationPage,
    ReconciliationRequest,
    ReconciliationVerdict,
    _canonical_json,
    _digest,
    _evaluation_archive_gate_metadata,
    _learning_effectiveness_summary,
    _learning_event,
    _now,
    evaluation_archive_entry,
)
from .runtime.errors import HarnessError


def _iso(value: Any) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value)


class LearningLedgerDependencyError(HarnessError):
    def __init__(self) -> None:
        super().__init__(
            code="LEARNING_LEDGER_DEPENDENCY_MISSING",
            category="learning",
            message=(
                "PostgreSQL learning storage requires the 'postgres' extra "
                "(psycopg and psycopg-pool)."
            ),
            retryable=False,
            details={"install_extra": "postgres"},
        )


class LearningLedgerOverloadedError(HarnessError):
    def __init__(self, *, retry_after_seconds: float) -> None:
        super().__init__(
            code="LEARNING_LEDGER_OVERLOADED",
            category="learning",
            message="The PostgreSQL learning connection pool is saturated.",
            retryable=True,
            details={"retry_after_seconds": retry_after_seconds},
        )


class LearningLedgerConnectionLostError(HarnessError):
    def __init__(self) -> None:
        super().__init__(
            code="LEARNING_LEDGER_CONNECTION_LOST",
            category="learning",
            message="The PostgreSQL learning-ledger connection was lost.",
            retryable=True,
            details={"backend": "postgres"},
        )


class PostgresLearningLedger:
    """Service learning ledger using row locks, CAS, and a bounded sync pool."""

    def __init__(
        self,
        dsn: str,
        *,
        min_pool_size: int = 1,
        max_pool_size: int = 10,
        max_waiting: int = 100,
        pool_timeout_seconds: float = 5.0,
        connect_timeout_seconds: float = 10.0,
        application_name: str = "agnoclaw-learning",
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("PostgreSQL DSN must be a non-empty string")
        if min_pool_size < 0 or max_pool_size <= 0 or min_pool_size > max_pool_size:
            raise ValueError("pool sizes require 0 <= min_pool_size <= max_pool_size")
        if max_waiting <= 0:
            raise ValueError("max_waiting must be positive")
        if pool_timeout_seconds <= 0 or connect_timeout_seconds <= 0:
            raise ValueError("pool and connect timeouts must be positive")
        try:
            import psycopg
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool, PoolTimeout, TooManyRequests
        except ImportError as exc:  # pragma: no cover - clean-wheel lane
            raise LearningLedgerDependencyError() from exc

        self.dsn = dsn
        self._fault_injector = fault_injector
        self._pool_timeout_seconds = pool_timeout_seconds
        self._pool_errors = (PoolTimeout, TooManyRequests)
        self._connection_errors = (psycopg.OperationalError, psycopg.InterfaceError)
        self._pool = ConnectionPool(
            conninfo=dsn,
            min_size=min_pool_size,
            max_size=max_pool_size,
            max_waiting=max_waiting,
            timeout=pool_timeout_seconds,
            kwargs={
                "autocommit": True,
                "row_factory": dict_row,
                "application_name": application_name,
            },
            check=ConnectionPool.check_connection,
            open=True,
            name="agnoclaw-learning",
        )
        try:
            self._pool.wait(timeout=connect_timeout_seconds)
            self.migrate()
        except BaseException:
            self._pool.close()
            raise

    def close(self) -> None:
        self._pool.close()

    def __enter__(self) -> PostgresLearningLedger:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    @property
    def pool_stats(self) -> dict[str, int]:
        return {key: int(value) for key, value in self._pool.get_stats().items()}

    def _fault(self, stage: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(stage)

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        try:
            with self._pool.connection(timeout=self._pool_timeout_seconds) as conn:
                yield conn
        except self._pool_errors as exc:
            raise LearningLedgerOverloadedError(
                retry_after_seconds=self._pool_timeout_seconds
            ) from exc
        except self._connection_errors as exc:
            raise LearningLedgerConnectionLostError() from exc

    @contextmanager
    def _transaction(self) -> Iterator[Any]:
        with self._connection() as conn:
            with conn.transaction():
                yield conn

    def migrate(self) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS learning_schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS learning_candidates (
                candidate_id TEXT PRIMARY KEY,
                candidate_digest TEXT NOT NULL,
                tenant_id TEXT,
                storage_namespace TEXT NOT NULL,
                state TEXT NOT NULL,
                revision BIGINT NOT NULL,
                content_storage_key TEXT NOT NULL,
                record_json TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS learning_candidates_owner_idx
            ON learning_candidates(
                tenant_id, storage_namespace, state, updated_at DESC
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS learning_evaluations (
                evaluation_id TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL REFERENCES learning_candidates(candidate_id)
                    ON DELETE CASCADE,
                evaluation_digest TEXT NOT NULL,
                evaluation_json TEXT NOT NULL,
                tenant_id TEXT,
                storage_namespace TEXT,
                target TEXT,
                mechanism_version TEXT,
                verdict TEXT,
                evaluator_digest TEXT,
                safety_passed BOOLEAN,
                reason_codes_json TEXT,
                created_at TIMESTAMPTZ NOT NULL
            )
            """,
            """
            ALTER TABLE learning_evaluations
            ADD COLUMN IF NOT EXISTS tenant_id TEXT,
            ADD COLUMN IF NOT EXISTS storage_namespace TEXT,
            ADD COLUMN IF NOT EXISTS target TEXT,
            ADD COLUMN IF NOT EXISTS mechanism_version TEXT,
            ADD COLUMN IF NOT EXISTS verdict TEXT,
            ADD COLUMN IF NOT EXISTS evaluator_digest TEXT,
            ADD COLUMN IF NOT EXISTS safety_passed BOOLEAN,
            ADD COLUMN IF NOT EXISTS reason_codes_json TEXT
            """,
            """
            CREATE INDEX IF NOT EXISTS learning_evaluations_candidate_idx
            ON learning_evaluations(candidate_id, created_at DESC, evaluation_id DESC)
            """,
            """
            CREATE TABLE IF NOT EXISTS learning_applications (
                application_id TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL REFERENCES learning_candidates(candidate_id)
                    ON DELETE CASCADE,
                run_id TEXT NOT NULL,
                application_kind TEXT NOT NULL,
                application_digest TEXT NOT NULL,
                application_json TEXT NOT NULL,
                observed_at TIMESTAMPTZ NOT NULL,
                UNIQUE(candidate_id, run_id, application_kind)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS learning_applications_candidate_idx
            ON learning_applications(
                candidate_id, observed_at DESC, application_id DESC
            )
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS learning_applications_run_kind_idx
            ON learning_applications(candidate_id, run_id, application_kind)
            """,
            """
            CREATE TABLE IF NOT EXISTS learning_outcomes (
                outcome_id TEXT PRIMARY KEY,
                application_id TEXT NOT NULL UNIQUE
                    REFERENCES learning_applications(application_id) ON DELETE CASCADE,
                candidate_id TEXT NOT NULL REFERENCES learning_candidates(candidate_id)
                    ON DELETE CASCADE,
                run_id TEXT NOT NULL,
                outcome_kind TEXT NOT NULL,
                score DOUBLE PRECISION NOT NULL,
                outcome_digest TEXT NOT NULL,
                outcome_json TEXT NOT NULL,
                recorded_at TIMESTAMPTZ NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS learning_outcomes_candidate_idx
            ON learning_outcomes(candidate_id, recorded_at DESC, outcome_id DESC)
            """,
            """
            CREATE TABLE IF NOT EXISTS learning_evaluation_reasons (
                evaluation_id TEXT NOT NULL REFERENCES learning_evaluations(evaluation_id)
                    ON DELETE CASCADE,
                reason_code TEXT NOT NULL,
                candidate_id TEXT NOT NULL REFERENCES learning_candidates(candidate_id)
                    ON DELETE CASCADE,
                created_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY(evaluation_id, reason_code)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS learning_reconciliations (
                reconciliation_id TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL REFERENCES learning_candidates(candidate_id)
                    ON DELETE CASCADE,
                reconciliation_digest TEXT NOT NULL,
                reconciliation_json TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS learning_reconciliations_candidate_idx
            ON learning_reconciliations(
                candidate_id, created_at DESC, reconciliation_id
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS learning_candidate_mutations (
                candidate_id TEXT NOT NULL REFERENCES learning_candidates(candidate_id)
                    ON DELETE CASCADE,
                mutation_id TEXT NOT NULL,
                mutation_digest TEXT NOT NULL,
                record_json TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY(candidate_id, mutation_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS learning_events (
                candidate_id TEXT NOT NULL REFERENCES learning_candidates(candidate_id)
                    ON DELETE CASCADE,
                sequence BIGINT NOT NULL,
                event_id TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL,
                occurred_at TIMESTAMPTZ NOT NULL,
                event_json TEXT NOT NULL,
                PRIMARY KEY(candidate_id, sequence)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS learning_outbox (
                outbox_id BIGSERIAL PRIMARY KEY,
                candidate_id TEXT NOT NULL,
                sequence BIGINT NOT NULL,
                event_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                lease_owner TEXT,
                lease_token TEXT,
                lease_expires_at TIMESTAMPTZ,
                delivered_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL,
                UNIQUE(candidate_id, sequence),
                FOREIGN KEY(candidate_id, sequence)
                    REFERENCES learning_events(candidate_id, sequence)
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS learning_outbox_ready_idx
            ON learning_outbox(status, lease_expires_at, outbox_id)
            """,
            """
            CREATE TABLE IF NOT EXISTS learning_reconciliation_workers (
                owner_digest TEXT PRIMARY KEY,
                worker_id TEXT,
                lease_token TEXT,
                lease_fence BIGINT NOT NULL DEFAULT 0,
                lease_expires_at TIMESTAMPTZ NOT NULL DEFAULT '-infinity',
                cursor_json TEXT,
                updated_at TIMESTAMPTZ NOT NULL
            )
            """,
        )
        with self._transaction() as conn:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ("agnoclaw.learning.schema",),
            )
            for statement in statements:
                conn.execute(statement)
            self._migrate_evaluation_archive_v5(conn)
            for version in range(1, LEARNING_LEDGER_SCHEMA_VERSION + 1):
                conn.execute(
                    """
                    INSERT INTO learning_schema_migrations(version, applied_at)
                    VALUES (%s, CURRENT_TIMESTAMP)
                    ON CONFLICT (version) DO NOTHING
                    """,
                    (version,),
                )

    @staticmethod
    def _migrate_evaluation_archive_v5(conn: Any) -> None:
        conn.execute(
            """
            UPDATE learning_evaluations AS e
            SET tenant_id = c.tenant_id,
                storage_namespace = c.storage_namespace,
                target = c.record_json::jsonb #>> '{candidate,target}',
                mechanism_version = c.record_json::jsonb #>>
                    '{candidate,mechanism_version}',
                verdict = e.evaluation_json::jsonb ->> 'verdict',
                evaluator_digest = e.evaluation_json::jsonb ->> 'evaluator_digest',
                safety_passed = (
                    e.evaluation_json::jsonb ->> 'safety_passed'
                )::boolean
            FROM learning_candidates AS c
            WHERE c.candidate_id = e.candidate_id
              AND (
                  e.storage_namespace IS NULL
                  OR e.target IS NULL
                  OR e.mechanism_version IS NULL
                  OR e.verdict IS NULL
                  OR e.evaluator_digest IS NULL
                  OR e.safety_passed IS NULL
              )
            """
        )
        with conn.cursor(name="agnoclaw_learning_archive_v5_backfill") as cursor:
            cursor.execute(
                """
                SELECT evaluation_id, candidate_id, evaluation_json, created_at
                FROM learning_evaluations
                WHERE reason_codes_json IS NULL
                ORDER BY evaluation_id
                """
            )
            while rows := cursor.fetchmany(1_000):
                reason_rows: list[tuple[str, str, str, Any]] = []
                marker_rows: list[tuple[str, str]] = []
                for row in rows:
                    evaluation = CandidateEvaluation.from_dict(json.loads(row["evaluation_json"]))
                    reasons, _ = _evaluation_archive_gate_metadata(evaluation)
                    marker_rows.append((_canonical_json(list(reasons)), str(row["evaluation_id"])))
                    reason_rows.extend(
                        (
                            str(row["evaluation_id"]),
                            reason,
                            str(row["candidate_id"]),
                            row["created_at"],
                        )
                        for reason in reasons
                    )
                with conn.cursor() as writer:
                    writer.executemany(
                        """
                        UPDATE learning_evaluations SET reason_codes_json = %s
                        WHERE evaluation_id = %s
                        """,
                        marker_rows,
                    )
                    writer.executemany(
                        """
                        INSERT INTO learning_evaluation_reasons(
                            evaluation_id, reason_code, candidate_id, created_at
                        ) VALUES (%s, %s, %s, %s)
                        ON CONFLICT (evaluation_id, reason_code) DO NOTHING
                        """,
                        reason_rows,
                    )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS learning_evaluations_archive_owner_idx
            ON learning_evaluations(
                tenant_id, storage_namespace, created_at DESC, evaluation_id DESC
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS learning_evaluations_archive_filter_idx
            ON learning_evaluations(
                tenant_id, storage_namespace, verdict, evaluator_digest,
                safety_passed, target, mechanism_version,
                created_at DESC, evaluation_id DESC
            )
            """
        )

    @property
    def schema_version(self) -> int:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM learning_schema_migrations"
            ).fetchone()
        return int(row["version"])

    @staticmethod
    def _record(row: Any) -> CandidateRecord:
        return CandidateRecord.from_dict(json.loads(row["record_json"]))

    @staticmethod
    def _owner_matches(record: CandidateRecord, owner: LearningOwner) -> bool:
        return (
            record.candidate.tenant_id == owner.tenant_id
            and record.candidate.storage_namespace == owner.storage_namespace
        )

    def _get_locked(
        self,
        conn: Any,
        candidate_id: str,
        *,
        owner: LearningOwner,
        for_update: bool = True,
    ) -> CandidateRecord:
        suffix = " FOR UPDATE" if for_update else ""
        row = conn.execute(
            "SELECT record_json FROM learning_candidates WHERE candidate_id = %s" + suffix,
            (candidate_id,),
        ).fetchone()
        if row is None:
            raise CandidateNotFoundError(candidate_id)
        record = self._record(row)
        if not self._owner_matches(record, owner):
            raise CandidateNotFoundError(candidate_id)
        return record

    @staticmethod
    def _save_event(
        conn: Any,
        before: CandidateRecord | None,
        after: CandidateRecord,
    ) -> None:
        event = _learning_event(before, after)
        event_json = _canonical_json(event.to_dict())
        conn.execute(
            """
            INSERT INTO learning_events(
                candidate_id, sequence, event_id, event_type, occurred_at, event_json
            ) VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                event.candidate_id,
                event.sequence,
                event.event_id,
                event.event_type,
                event.occurred_at,
                event_json,
            ),
        )
        conn.execute(
            """
            INSERT INTO learning_outbox(
                candidate_id, sequence, event_json, status, created_at
            ) VALUES (%s, %s, %s, 'pending', %s)
            """,
            (event.candidate_id, event.sequence, event_json, event.occurred_at),
        )

    def create_candidate(self, candidate: LearningCandidate) -> CandidateRecord:
        record = CandidateRecord(candidate=candidate)
        with self._transaction() as conn:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"agnoclaw.learning.candidate:{candidate.candidate_id}",),
            )
            existing = conn.execute(
                """
                SELECT candidate_digest, record_json FROM learning_candidates
                WHERE candidate_id = %s FOR UPDATE
                """,
                (candidate.candidate_id,),
            ).fetchone()
            if existing is not None:
                if existing["candidate_digest"] != candidate.digest:
                    raise CandidateConflictError(candidate.candidate_id)
                return self._record(existing)
            if candidate.supersedes_candidate_id is not None:
                parent = self._get_locked(
                    conn,
                    candidate.supersedes_candidate_id,
                    owner=candidate.owner,
                )
                if parent.state is CandidateState.DELETED:
                    raise CandidateTransitionError(
                        candidate.supersedes_candidate_id,
                        state=parent.state,
                        action="supersede",
                    )
                if parent.candidate.target is not candidate.target:
                    raise HarnessError(
                        code="LEARNING_CANDIDATE_TARGET_CONFLICT",
                        category="learning",
                        message="An edited candidate cannot change learning target.",
                        retryable=False,
                        details={"candidate_id": candidate.candidate_id},
                    )
            conn.execute(
                """
                INSERT INTO learning_candidates(
                    candidate_id, candidate_digest, tenant_id, storage_namespace,
                    state, revision, content_storage_key, record_json, created_at,
                    updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    candidate.candidate_id,
                    candidate.digest,
                    candidate.tenant_id,
                    candidate.storage_namespace,
                    record.state.value,
                    record.revision,
                    candidate.content_artifact.storage_key,
                    _canonical_json(record.to_dict()),
                    candidate.created_at,
                    record.updated_at,
                ),
            )
            self._save_event(conn, None, record)
        return record

    def get_candidate(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
    ) -> CandidateRecord:
        with self._connection() as conn:
            return self._get_locked(
                conn,
                candidate_id,
                owner=owner,
                for_update=False,
            )

    def list_candidates(
        self,
        *,
        owner: LearningOwner,
        limit: int = 100,
        state: CandidateState | None = None,
    ) -> list[CandidateRecord]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        params: list[Any] = [owner.tenant_id, owner.storage_namespace]
        state_clause = ""
        if state is not None:
            state_clause = " AND state = %s"
            params.append(CandidateState(state).value)
        params.append(limit)
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT record_json FROM learning_candidates
                WHERE tenant_id IS NOT DISTINCT FROM %s
                  AND storage_namespace = %s
                """
                + state_clause
                + " ORDER BY created_at DESC, candidate_id LIMIT %s",
                params,
            ).fetchall()
        return [self._record(row) for row in rows]

    def scan_reconciliation_required(
        self,
        *,
        owner: LearningOwner,
        limit: int = 100,
        cursor: ReconciliationCursor | None = None,
    ) -> ReconciliationPage:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if cursor is not None and cursor.owner_digest != owner.digest:
            raise ReconciliationCursorScopeError()
        params: list[Any] = [
            owner.tenant_id,
            owner.storage_namespace,
            CandidateState.PROMOTION_UNKNOWN.value,
            CandidateState.ROLLBACK_UNKNOWN.value,
        ]
        cursor_clause = ""
        if cursor is not None:
            cursor_clause = " AND (updated_at > %s OR (updated_at = %s AND candidate_id > %s))"
            params.extend([cursor.updated_at, cursor.updated_at, cursor.candidate_id])
        params.append(limit + 1)
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT record_json, updated_at, candidate_id
                FROM learning_candidates
                WHERE tenant_id IS NOT DISTINCT FROM %s
                  AND storage_namespace = %s
                  AND state IN (%s, %s)
                """
                + cursor_clause
                + " ORDER BY updated_at ASC, candidate_id ASC LIMIT %s",
                params,
            ).fetchall()
        selected = rows[:limit]
        requests = tuple(
            ReconciliationRequest(
                record=(record := self._record(row)),
                kind=(
                    ReconciliationKind.PROMOTION
                    if record.state is CandidateState.PROMOTION_UNKNOWN
                    else ReconciliationKind.ROLLBACK
                ),
            )
            for row in selected
        )
        next_cursor = None
        if len(rows) > limit:
            last = selected[-1]
            next_cursor = ReconciliationCursor(
                updated_at=_iso(last["updated_at"]),
                candidate_id=str(last["candidate_id"]),
                owner_digest=owner.digest,
            )
        return ReconciliationPage(items=requests, next_cursor=next_cursor)

    def get_evaluation(
        self,
        evaluation_id: str,
        *,
        owner: LearningOwner,
    ) -> CandidateEvaluation:
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT candidate_id, evaluation_json FROM learning_evaluations
                WHERE evaluation_id = %s
                """,
                (evaluation_id,),
            ).fetchone()
            if row is None:
                raise CandidateEvaluationNotFoundError(evaluation_id)
            try:
                self._get_locked(
                    conn,
                    str(row["candidate_id"]),
                    owner=owner,
                    for_update=False,
                )
            except CandidateNotFoundError as exc:
                raise CandidateEvaluationNotFoundError(evaluation_id) from exc
        return CandidateEvaluation.from_dict(json.loads(row["evaluation_json"]))

    def list_evaluations(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
        limit: int = 100,
    ) -> list[CandidateEvaluation]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._connection() as conn:
            self._get_locked(
                conn,
                candidate_id,
                owner=owner,
                for_update=False,
            )
            rows = conn.execute(
                """
                SELECT evaluation_json FROM learning_evaluations
                WHERE candidate_id = %s
                ORDER BY created_at DESC, evaluation_id LIMIT %s
                """,
                (candidate_id, limit),
            ).fetchall()
        return [CandidateEvaluation.from_dict(json.loads(row["evaluation_json"])) for row in rows]

    def record_application(
        self,
        application: LearningApplication,
        *,
        owner: LearningOwner,
    ) -> LearningApplication:
        if not isinstance(application, LearningApplication):
            raise TypeError("application must be a LearningApplication")
        with self._transaction() as conn:
            candidate = self._get_locked(conn, application.candidate_id, owner=owner)
            existing = conn.execute(
                """
                SELECT application_digest, application_json
                FROM learning_applications
                WHERE application_id = %s
                   OR (
                       candidate_id = %s AND run_id = %s AND application_kind = %s
                   )
                """,
                (
                    application.application_id,
                    application.candidate_id,
                    application.run_id,
                    application.kind.value,
                ),
            ).fetchone()
            if existing is not None:
                if existing["application_digest"] != application.digest:
                    raise LearningAttributionConflictError(record_id=application.application_id)
                return LearningApplication.from_dict(json.loads(existing["application_json"]))
            if candidate.state is not CandidateState.PROMOTED:
                raise CandidateTransitionError(
                    application.candidate_id,
                    state=candidate.state,
                    action="record application",
                )
            if candidate.target_reference != application.target_reference:
                raise HarnessError(
                    code="LEARNING_APPLICATION_TARGET_MISMATCH",
                    category="learning",
                    message="Application evidence does not match the promoted target.",
                    retryable=False,
                    details={"candidate_id": application.candidate_id},
                )
            if candidate.candidate.expires_at is not None and datetime.fromisoformat(
                candidate.candidate.expires_at
            ) <= datetime.now(UTC):
                raise HarnessError(
                    code="LEARNING_APPLICATION_EXPIRED",
                    category="learning",
                    message="An expired learning cannot receive new application evidence.",
                    retryable=False,
                    details={"candidate_id": application.candidate_id},
                )
            conn.execute(
                """
                INSERT INTO learning_applications(
                    application_id, candidate_id, run_id, application_kind,
                    application_digest, application_json, observed_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    application.application_id,
                    application.candidate_id,
                    application.run_id,
                    application.kind.value,
                    application.digest,
                    _canonical_json(application.to_dict()),
                    application.observed_at,
                ),
            )
        return application

    def get_application(
        self,
        application_id: str,
        *,
        owner: LearningOwner,
    ) -> LearningApplication:
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT candidate_id, application_json FROM learning_applications
                WHERE application_id = %s
                """,
                (application_id,),
            ).fetchone()
            if row is None:
                raise LearningApplicationNotFoundError(application_id)
            try:
                self._get_locked(
                    conn,
                    str(row["candidate_id"]),
                    owner=owner,
                    for_update=False,
                )
            except CandidateNotFoundError as exc:
                raise LearningApplicationNotFoundError(application_id) from exc
        return LearningApplication.from_dict(json.loads(row["application_json"]))

    def list_applications(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
        limit: int = 100,
    ) -> list[LearningApplication]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._connection() as conn:
            self._get_locked(conn, candidate_id, owner=owner, for_update=False)
            rows = conn.execute(
                """
                SELECT application_json FROM learning_applications
                WHERE candidate_id = %s
                ORDER BY observed_at DESC, application_id DESC LIMIT %s
                """,
                (candidate_id, limit),
            ).fetchall()
        return [LearningApplication.from_dict(json.loads(row["application_json"])) for row in rows]

    def record_outcome(
        self,
        outcome: LearningOutcome,
        *,
        owner: LearningOwner,
    ) -> LearningOutcome:
        if not isinstance(outcome, LearningOutcome):
            raise TypeError("outcome must be a LearningOutcome")
        with self._transaction() as conn:
            candidate = self._get_locked(conn, outcome.candidate_id, owner=owner)
            if candidate.state is CandidateState.DELETED:
                raise CandidateTransitionError(
                    outcome.candidate_id,
                    state=candidate.state,
                    action="record outcome",
                )
            application_row = conn.execute(
                """
                SELECT application_json FROM learning_applications
                WHERE application_id = %s AND candidate_id = %s
                FOR UPDATE
                """,
                (outcome.application_id, outcome.candidate_id),
            ).fetchone()
            if application_row is None:
                raise LearningApplicationNotFoundError(outcome.application_id)
            existing = conn.execute(
                """
                SELECT outcome_digest, outcome_json FROM learning_outcomes
                WHERE outcome_id = %s OR application_id = %s
                FOR UPDATE
                """,
                (outcome.outcome_id, outcome.application_id),
            ).fetchone()
            if existing is not None:
                if existing["outcome_digest"] != outcome.digest:
                    raise LearningAttributionConflictError(record_id=outcome.outcome_id)
                return LearningOutcome.from_dict(json.loads(existing["outcome_json"]))
            application = LearningApplication.from_dict(
                json.loads(application_row["application_json"])
            )
            if application.kind is not LearningApplicationKind.APPLIED:
                raise HarnessError(
                    code="LEARNING_OUTCOME_NOT_APPLIED",
                    category="learning",
                    message="An outcome can only be attributed to an applied learning.",
                    retryable=False,
                    details={"application_id": outcome.application_id},
                )
            if application.run_id != outcome.run_id:
                raise HarnessError(
                    code="LEARNING_OUTCOME_RUN_MISMATCH",
                    category="learning",
                    message="Outcome evidence must match the attributed run.",
                    retryable=False,
                    details={"application_id": outcome.application_id},
                )
            if application.observer_digest == outcome.evaluator_digest or set(
                application.evidence_artifact_ids
            ).intersection(outcome.evidence_artifact_ids):
                raise HarnessError(
                    code="LEARNING_OUTCOME_INDEPENDENCE_REQUIRED",
                    category="learning",
                    message="Outcome evaluation must use a distinct evaluator and evidence.",
                    retryable=False,
                    details={"application_id": outcome.application_id},
                )
            conn.execute(
                """
                INSERT INTO learning_outcomes(
                    outcome_id, application_id, candidate_id, run_id, outcome_kind,
                    score, outcome_digest, outcome_json, recorded_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    outcome.outcome_id,
                    outcome.application_id,
                    outcome.candidate_id,
                    outcome.run_id,
                    outcome.kind.value,
                    outcome.score,
                    outcome.digest,
                    _canonical_json(outcome.to_dict()),
                    outcome.recorded_at,
                ),
            )
        return outcome

    def get_outcome(
        self,
        outcome_id: str,
        *,
        owner: LearningOwner,
    ) -> LearningOutcome:
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT candidate_id, outcome_json FROM learning_outcomes
                WHERE outcome_id = %s
                """,
                (outcome_id,),
            ).fetchone()
            if row is None:
                raise LearningOutcomeNotFoundError(outcome_id)
            try:
                self._get_locked(
                    conn,
                    str(row["candidate_id"]),
                    owner=owner,
                    for_update=False,
                )
            except CandidateNotFoundError as exc:
                raise LearningOutcomeNotFoundError(outcome_id) from exc
        return LearningOutcome.from_dict(json.loads(row["outcome_json"]))

    def list_outcomes(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
        limit: int = 100,
    ) -> list[LearningOutcome]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._connection() as conn:
            self._get_locked(conn, candidate_id, owner=owner, for_update=False)
            rows = conn.execute(
                """
                SELECT outcome_json FROM learning_outcomes
                WHERE candidate_id = %s
                ORDER BY recorded_at DESC, outcome_id DESC LIMIT %s
                """,
                (candidate_id, limit),
            ).fetchall()
        return [LearningOutcome.from_dict(json.loads(row["outcome_json"])) for row in rows]

    def summarize_effectiveness(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
        policy: LearningEffectivenessPolicy,
    ) -> LearningEffectivenessSummary:
        if not isinstance(policy, LearningEffectivenessPolicy):
            raise TypeError("policy must be a LearningEffectivenessPolicy")
        with self._connection() as conn:
            self._get_locked(conn, candidate_id, owner=owner, for_update=False)
            applications = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE application_kind = 'applied') AS applied
                FROM learning_applications WHERE candidate_id = %s
                """,
                (candidate_id,),
            ).fetchone()
            outcomes = conn.execute(
                """
                SELECT COUNT(*) AS total, COUNT(DISTINCT run_id) AS runs,
                       COUNT(*) FILTER (WHERE outcome_kind = 'success') AS successes,
                       COUNT(*) FILTER (WHERE outcome_kind = 'failure') AS failures,
                       COUNT(*) FILTER (WHERE outcome_kind = 'correction') AS corrections,
                       COUNT(*) FILTER (WHERE outcome_kind = 'neutral') AS neutral,
                       SUM(score) AS score_sum
                FROM learning_outcomes WHERE candidate_id = %s
                """,
                (candidate_id,),
            ).fetchone()
        return _learning_effectiveness_summary(
            candidate_id=candidate_id,
            total_applications=int(applications["total"] or 0),
            applied_applications=int(applications["applied"] or 0),
            evaluated_outcomes=int(outcomes["total"] or 0),
            independent_runs=int(outcomes["runs"] or 0),
            successes=int(outcomes["successes"] or 0),
            failures=int(outcomes["failures"] or 0),
            corrections=int(outcomes["corrections"] or 0),
            neutral=int(outcomes["neutral"] or 0),
            score_sum=(float(outcomes["score_sum"]) if outcomes["score_sum"] is not None else None),
            policy=policy,
        )

    def query_evaluation_archive(
        self,
        *,
        owner: LearningOwner,
        query: EvaluationArchiveQuery,
    ) -> EvaluationArchivePage:
        if not isinstance(query, EvaluationArchiveQuery):
            raise TypeError("query must be an EvaluationArchiveQuery")
        if query.cursor is not None and query.cursor.owner_digest != owner.digest:
            raise HarnessError(
                code="LEARNING_EVALUATION_ARCHIVE_CURSOR_SCOPE",
                category="learning",
                message="The evaluation archive cursor belongs to another owner.",
                retryable=False,
            )
        verdict_placeholders = ",".join("%s" for _ in query.verdicts)
        sql = f"""
            SELECT e.evaluation_json, c.record_json, e.created_at, e.evaluation_id
            FROM learning_candidates AS c
            JOIN learning_evaluations AS e ON e.candidate_id = c.candidate_id
            WHERE e.tenant_id IS NOT DISTINCT FROM %s
              AND e.storage_namespace = %s
              AND e.verdict IN ({verdict_placeholders})
        """
        params: list[Any] = [
            owner.tenant_id,
            owner.storage_namespace,
            *(item.value for item in query.verdicts),
        ]
        if query.evaluator_digest is not None:
            sql += " AND e.evaluator_digest = %s"
            params.append(query.evaluator_digest)
        if query.reason_code is not None:
            sql += """
                AND EXISTS (
                    SELECT 1 FROM learning_evaluation_reasons AS reason
                    WHERE reason.evaluation_id = e.evaluation_id
                      AND reason.reason_code = %s
                )
            """
            params.append(query.reason_code)
        if query.mechanism_version is not None:
            sql += " AND e.mechanism_version = %s"
            params.append(query.mechanism_version)
        if query.target is not None:
            sql += " AND e.target = %s"
            params.append(query.target.value)
        if query.safety_passed is not None:
            sql += " AND e.safety_passed = %s"
            params.append(query.safety_passed)
        if query.cursor is not None:
            sql += """
                AND (
                    e.created_at < %s
                    OR (e.created_at = %s AND e.evaluation_id < %s)
                )
            """
            params.extend(
                [
                    query.cursor.evaluated_at,
                    query.cursor.evaluated_at,
                    query.cursor.evaluation_id,
                ]
            )
        sql += " ORDER BY e.created_at DESC, e.evaluation_id DESC LIMIT %s"
        params.append(query.limit + 1)
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        selected = rows[: query.limit]
        items = tuple(
            evaluation_archive_entry(
                self._record(row),
                CandidateEvaluation.from_dict(json.loads(row["evaluation_json"])),
            )
            for row in selected
        )
        next_cursor = None
        if len(rows) > query.limit:
            last = selected[-1]
            next_cursor = EvaluationArchiveCursor(
                evaluated_at=_iso(last["created_at"]),
                evaluation_id=str(last["evaluation_id"]),
                owner_digest=owner.digest,
            )
        return EvaluationArchivePage(items=items, next_cursor=next_cursor)

    def get_reconciliation(
        self,
        reconciliation_id: str,
        *,
        owner: LearningOwner,
    ) -> CandidateReconciliation:
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT candidate_id, reconciliation_json FROM learning_reconciliations
                WHERE reconciliation_id = %s
                """,
                (reconciliation_id,),
            ).fetchone()
            if row is None:
                raise CandidateReconciliationNotFoundError(reconciliation_id)
            try:
                self._get_locked(
                    conn,
                    str(row["candidate_id"]),
                    owner=owner,
                    for_update=False,
                )
            except CandidateNotFoundError as exc:
                raise CandidateReconciliationNotFoundError(reconciliation_id) from exc
        return CandidateReconciliation.from_dict(json.loads(row["reconciliation_json"]))

    def list_reconciliations(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
        limit: int = 100,
    ) -> list[CandidateReconciliation]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._connection() as conn:
            self._get_locked(
                conn,
                candidate_id,
                owner=owner,
                for_update=False,
            )
            rows = conn.execute(
                """
                SELECT reconciliation_json FROM learning_reconciliations
                WHERE candidate_id = %s
                ORDER BY created_at DESC, reconciliation_id LIMIT %s
                """,
                (candidate_id, limit),
            ).fetchall()
        return [
            CandidateReconciliation.from_dict(json.loads(row["reconciliation_json"]))
            for row in rows
        ]

    @staticmethod
    def _idempotent_mutation(
        conn: Any,
        candidate_id: str,
        *,
        mutation_id: str,
        mutation_digest: str,
    ) -> CandidateRecord | None:
        row = conn.execute(
            """
            SELECT mutation_digest, record_json FROM learning_candidate_mutations
            WHERE candidate_id = %s AND mutation_id = %s
            """,
            (candidate_id, mutation_id),
        ).fetchone()
        if row is None:
            return None
        if row["mutation_digest"] != mutation_digest:
            raise CandidateConflictError(candidate_id)
        return CandidateRecord.from_dict(json.loads(row["record_json"]))

    def _save_mutation(
        self,
        conn: Any,
        before: CandidateRecord,
        after: CandidateRecord,
        *,
        mutation_id: str,
        mutation_digest: str,
    ) -> None:
        updated = conn.execute(
            """
            UPDATE learning_candidates
            SET state = %s, revision = %s, record_json = %s, updated_at = %s
            WHERE candidate_id = %s AND revision = %s
            """,
            (
                after.state.value,
                after.revision,
                _canonical_json(after.to_dict()),
                after.updated_at,
                after.candidate.candidate_id,
                before.revision,
            ),
        )
        if updated.rowcount != 1:
            current = self._get_locked(
                conn,
                before.candidate.candidate_id,
                owner=before.candidate.owner,
            )
            raise CandidateRevisionError(
                before.candidate.candidate_id,
                expected=before.revision,
                actual=current.revision,
            )
        self._fault("after_candidate_update")
        conn.execute(
            """
            INSERT INTO learning_candidate_mutations(
                candidate_id, mutation_id, mutation_digest, record_json, created_at
            ) VALUES (%s, %s, %s, %s, %s)
            """,
            (
                after.candidate.candidate_id,
                mutation_id,
                mutation_digest,
                _canonical_json(after.to_dict()),
                after.updated_at,
            ),
        )
        self._save_event(conn, before, after)

    def record_evaluation(
        self,
        evaluation: CandidateEvaluation,
        *,
        owner: LearningOwner,
        expected_revision: int,
        mutation_id: str,
    ) -> CandidateRecord:
        mutation_digest = _digest({"action": "evaluate", "evaluation_digest": evaluation.digest})
        with self._transaction() as conn:
            before = self._get_locked(conn, evaluation.candidate_id, owner=owner)
            replay = self._idempotent_mutation(
                conn,
                evaluation.candidate_id,
                mutation_id=mutation_id,
                mutation_digest=mutation_digest,
            )
            if replay is not None:
                return replay
            if before.revision != expected_revision:
                raise CandidateRevisionError(
                    evaluation.candidate_id,
                    expected=expected_revision,
                    actual=before.revision,
                )
            if before.state not in {
                CandidateState.CAPTURED,
                CandidateState.QUALIFIED,
                CandidateState.REJECTED,
            }:
                raise CandidateTransitionError(
                    evaluation.candidate_id,
                    state=before.state,
                    action="evaluate",
                )
            next_state = {
                EvaluationVerdict.QUALIFIED: CandidateState.QUALIFIED,
                EvaluationVerdict.REJECTED: CandidateState.REJECTED,
                EvaluationVerdict.INCONCLUSIVE: CandidateState.CAPTURED,
            }[evaluation.verdict]
            after = replace(
                before,
                state=next_state,
                revision=before.revision + 1,
                latest_evaluation_id=evaluation.evaluation_id,
                updated_at=evaluation.evaluated_at,
            )
            reason_codes, _ = _evaluation_archive_gate_metadata(evaluation)
            conn.execute(
                """
                INSERT INTO learning_evaluations(
                    evaluation_id, candidate_id, evaluation_digest,
                    evaluation_json, tenant_id, storage_namespace, target,
                    mechanism_version, verdict, evaluator_digest, safety_passed,
                    reason_codes_json, created_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    evaluation.evaluation_id,
                    evaluation.candidate_id,
                    evaluation.digest,
                    _canonical_json(evaluation.to_dict()),
                    owner.tenant_id,
                    owner.storage_namespace,
                    before.candidate.target.value,
                    before.candidate.mechanism_version,
                    evaluation.verdict.value,
                    evaluation.evaluator_digest,
                    evaluation.safety_passed,
                    _canonical_json(list(reason_codes)),
                    evaluation.evaluated_at,
                ),
            )
            with conn.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO learning_evaluation_reasons(
                        evaluation_id, reason_code, candidate_id, created_at
                    ) VALUES (%s, %s, %s, %s)
                    """,
                    [
                        (
                            evaluation.evaluation_id,
                            reason,
                            evaluation.candidate_id,
                            evaluation.evaluated_at,
                        )
                        for reason in reason_codes
                    ],
                )
            self._save_mutation(
                conn,
                before,
                after,
                mutation_id=mutation_id,
                mutation_digest=mutation_digest,
            )
        return after

    def record_reconciliation(
        self,
        reconciliation: CandidateReconciliation,
        *,
        owner: LearningOwner,
        expected_revision: int,
        mutation_id: str,
    ) -> CandidateRecord:
        mutation_digest = _digest(
            {"action": "reconcile", "reconciliation_digest": reconciliation.digest}
        )
        with self._transaction() as conn:
            before = self._get_locked(conn, reconciliation.candidate_id, owner=owner)
            replay = self._idempotent_mutation(
                conn,
                reconciliation.candidate_id,
                mutation_id=mutation_id,
                mutation_digest=mutation_digest,
            )
            if replay is not None:
                return replay
            existing = conn.execute(
                """
                SELECT reconciliation_digest FROM learning_reconciliations
                WHERE reconciliation_id = %s
                """,
                (reconciliation.reconciliation_id,),
            ).fetchone()
            if existing is not None:
                if existing["reconciliation_digest"] != reconciliation.digest:
                    raise CandidateConflictError(reconciliation.candidate_id)
                return before
            self._require_revision(before, expected_revision)
            if reconciliation.kind is ReconciliationKind.PROMOTION:
                allowed = {
                    CandidateState.PROMOTING,
                    CandidateState.PROMOTION_UNKNOWN,
                }
                next_state = (
                    CandidateState.PROMOTED
                    if reconciliation.verdict is ReconciliationVerdict.EFFECT_PRESENT
                    else CandidateState.QUALIFIED
                )
                target_reference = (
                    reconciliation.target_reference
                    if next_state is CandidateState.PROMOTED
                    else None
                )
            else:
                allowed = {
                    CandidateState.ROLLING_BACK,
                    CandidateState.ROLLBACK_UNKNOWN,
                }
                next_state = (
                    CandidateState.PROMOTED
                    if reconciliation.verdict is ReconciliationVerdict.EFFECT_PRESENT
                    else CandidateState.ROLLED_BACK
                )
                target_reference = before.target_reference
            if before.state not in allowed:
                raise CandidateTransitionError(
                    reconciliation.candidate_id,
                    state=before.state,
                    action=f"reconcile {reconciliation.kind.value}",
                )
            after = replace(
                before,
                state=next_state,
                revision=before.revision + 1,
                target_reference=target_reference,
                latest_reconciliation_id=reconciliation.reconciliation_id,
                updated_at=reconciliation.reconciled_at,
            )
            conn.execute(
                """
                INSERT INTO learning_reconciliations(
                    reconciliation_id, candidate_id, reconciliation_digest,
                    reconciliation_json, created_at
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    reconciliation.reconciliation_id,
                    reconciliation.candidate_id,
                    reconciliation.digest,
                    _canonical_json(reconciliation.to_dict()),
                    reconciliation.reconciled_at,
                ),
            )
            self._save_mutation(
                conn,
                before,
                after,
                mutation_id=mutation_id,
                mutation_digest=mutation_digest,
            )
        return after

    def begin_promotion(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
        expected_revision: int,
        mutation_id: str,
        promotion_id: str,
        actor: PromotionActor,
    ) -> CandidateRecord:
        actor = PromotionActor(actor)
        mutation_digest = _digest(
            {
                "action": "begin_promotion",
                "promotion_id": promotion_id,
                "actor": actor.value,
            }
        )
        with self._transaction() as conn:
            before = self._get_locked(conn, candidate_id, owner=owner)
            replay = self._idempotent_mutation(
                conn,
                candidate_id,
                mutation_id=mutation_id,
                mutation_digest=mutation_digest,
            )
            if replay is not None:
                return replay
            self._require_revision(before, expected_revision)
            if before.state is not CandidateState.QUALIFIED:
                raise CandidateTransitionError(
                    candidate_id,
                    state=before.state,
                    action="promote",
                )
            after = replace(
                before,
                state=CandidateState.PROMOTING,
                revision=before.revision + 1,
                promotion_id=promotion_id,
                promotion_request_id=mutation_id.removesuffix(":begin"),
                promotion_actor=actor,
                promotion_version=before.promotion_version + 1,
                updated_at=_now(),
            )
            self._save_mutation(
                conn,
                before,
                after,
                mutation_id=mutation_id,
                mutation_digest=mutation_digest,
            )
        return after

    def settle_promotion(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
        expected_revision: int,
        mutation_id: str,
        succeeded: bool,
        target_reference: str | None,
    ) -> CandidateRecord:
        if succeeded and not target_reference:
            raise ValueError("successful promotion requires target_reference")
        mutation_digest = _digest(
            {
                "action": "settle_promotion",
                "succeeded": succeeded,
                "target_reference": target_reference,
            }
        )
        with self._transaction() as conn:
            before = self._get_locked(conn, candidate_id, owner=owner)
            replay = self._idempotent_mutation(
                conn,
                candidate_id,
                mutation_id=mutation_id,
                mutation_digest=mutation_digest,
            )
            if replay is not None:
                return replay
            self._require_revision(before, expected_revision)
            if before.state is not CandidateState.PROMOTING:
                raise CandidateTransitionError(
                    candidate_id,
                    state=before.state,
                    action="settle promotion for",
                )
            after = replace(
                before,
                state=(CandidateState.PROMOTED if succeeded else CandidateState.PROMOTION_UNKNOWN),
                revision=before.revision + 1,
                target_reference=target_reference,
                updated_at=_now(),
            )
            self._save_mutation(
                conn,
                before,
                after,
                mutation_id=mutation_id,
                mutation_digest=mutation_digest,
            )
        return after

    def transition_candidate(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
        expected_revision: int,
        mutation_id: str,
        action: CandidateAction,
    ) -> CandidateRecord:
        action = CandidateAction(action)
        mutation_digest = _digest({"action": action.value})
        with self._transaction() as conn:
            before = self._get_locked(conn, candidate_id, owner=owner)
            replay = self._idempotent_mutation(
                conn,
                candidate_id,
                mutation_id=mutation_id,
                mutation_digest=mutation_digest,
            )
            if replay is not None:
                return replay
            self._require_revision(before, expected_revision)
            next_state = _CANDIDATE_TRANSITIONS[action].get(before.state)
            if next_state is None:
                raise CandidateTransitionError(
                    candidate_id,
                    state=before.state,
                    action=action.value,
                )
            after = replace(
                before,
                state=next_state,
                revision=before.revision + 1,
                updated_at=_now(),
            )
            self._save_mutation(
                conn,
                before,
                after,
                mutation_id=mutation_id,
                mutation_digest=mutation_digest,
            )
        return after

    def begin_rollback(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
        expected_revision: int,
        mutation_id: str,
        rollback_id: str,
        actor: PromotionActor,
    ) -> CandidateRecord:
        actor = PromotionActor(actor)
        mutation_digest = _digest(
            {
                "action": "begin_rollback",
                "rollback_id": rollback_id,
                "actor": actor.value,
            }
        )
        with self._transaction() as conn:
            before = self._get_locked(conn, candidate_id, owner=owner)
            replay = self._idempotent_mutation(
                conn,
                candidate_id,
                mutation_id=mutation_id,
                mutation_digest=mutation_digest,
            )
            if replay is not None:
                return replay
            self._require_revision(before, expected_revision)
            if before.state is not CandidateState.PROMOTED:
                raise CandidateTransitionError(
                    candidate_id,
                    state=before.state,
                    action="roll back",
                )
            after = replace(
                before,
                state=CandidateState.ROLLING_BACK,
                revision=before.revision + 1,
                rollback_id=rollback_id,
                rollback_request_id=mutation_id.removesuffix(":begin"),
                rollback_actor=actor,
                updated_at=_now(),
            )
            self._save_mutation(
                conn,
                before,
                after,
                mutation_id=mutation_id,
                mutation_digest=mutation_digest,
            )
        return after

    def settle_rollback(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
        expected_revision: int,
        mutation_id: str,
        succeeded: bool,
    ) -> CandidateRecord:
        mutation_digest = _digest({"action": "settle_rollback", "succeeded": succeeded})
        with self._transaction() as conn:
            before = self._get_locked(conn, candidate_id, owner=owner)
            replay = self._idempotent_mutation(
                conn,
                candidate_id,
                mutation_id=mutation_id,
                mutation_digest=mutation_digest,
            )
            if replay is not None:
                return replay
            self._require_revision(before, expected_revision)
            if before.state is not CandidateState.ROLLING_BACK:
                raise CandidateTransitionError(
                    candidate_id,
                    state=before.state,
                    action="settle rollback for",
                )
            after = replace(
                before,
                state=(
                    CandidateState.ROLLED_BACK if succeeded else CandidateState.ROLLBACK_UNKNOWN
                ),
                revision=before.revision + 1,
                updated_at=_now(),
            )
            self._save_mutation(
                conn,
                before,
                after,
                mutation_id=mutation_id,
                mutation_digest=mutation_digest,
            )
        return after

    @staticmethod
    def _require_revision(record: CandidateRecord, expected: int) -> None:
        if record.revision != expected:
            raise CandidateRevisionError(
                record.candidate.candidate_id,
                expected=expected,
                actual=record.revision,
            )

    def list_artifact_storage_keys(self) -> list[str]:
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT content_storage_key FROM learning_candidates
                WHERE state != %s ORDER BY content_storage_key
                """,
                (CandidateState.DELETED.value,),
            ).fetchall()
        return [str(row["content_storage_key"]) for row in rows]

    def list_events(
        self,
        candidate_id: str,
        *,
        owner: LearningOwner,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> list[LearningEvent]:
        if after_sequence < 0:
            raise ValueError("after_sequence cannot be negative")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._connection() as conn:
            self._get_locked(
                conn,
                candidate_id,
                owner=owner,
                for_update=False,
            )
            rows = conn.execute(
                """
                SELECT event_json FROM learning_events
                WHERE candidate_id = %s AND sequence > %s
                ORDER BY sequence LIMIT %s
                """,
                (candidate_id, after_sequence, limit),
            ).fetchall()
        return [LearningEvent.from_dict(json.loads(row["event_json"])) for row in rows]

    def lease_outbox(
        self,
        *,
        owner: str,
        limit: int = 100,
        lease_seconds: int = 30,
    ) -> list[LearningOutboxItem]:
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("outbox owner must be a non-empty string")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        leased: list[LearningOutboxItem] = []
        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT outbox_id, event_json FROM learning_outbox
                WHERE delivered_at IS NULL
                  AND (status = 'pending' OR lease_expires_at <= CURRENT_TIMESTAMP)
                ORDER BY outbox_id
                FOR UPDATE SKIP LOCKED LIMIT %s
                """,
                (limit,),
            ).fetchall()
            for row in rows:
                token = f"learning-lease:{uuid4().hex}"
                updated = conn.execute(
                    """
                    UPDATE learning_outbox
                    SET status = 'leased', lease_owner = %s, lease_token = %s,
                        lease_expires_at = CURRENT_TIMESTAMP
                            + (%s * INTERVAL '1 second')
                    WHERE outbox_id = %s AND delivered_at IS NULL
                    RETURNING lease_expires_at
                    """,
                    (owner, token, lease_seconds, int(row["outbox_id"])),
                ).fetchone()
                if updated is None:  # pragma: no cover - row lock invariant
                    continue
                leased.append(
                    LearningOutboxItem(
                        outbox_id=int(row["outbox_id"]),
                        event=LearningEvent.from_dict(json.loads(row["event_json"])),
                        lease_owner=owner,
                        lease_token=token,
                        lease_expires_at=_iso(updated["lease_expires_at"]),
                    )
                )
        return leased

    def acknowledge_outbox(self, *, outbox_id: int, lease_token: str) -> None:
        if not isinstance(lease_token, str) or not lease_token.strip():
            raise ValueError("lease_token must be a non-empty string")
        with self._transaction() as conn:
            updated = conn.execute(
                """
                UPDATE learning_outbox
                SET status = 'delivered', delivered_at = CURRENT_TIMESTAMP,
                    lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL
                WHERE outbox_id = %s AND lease_token = %s AND delivered_at IS NULL
                  AND lease_expires_at > CURRENT_TIMESTAMP
                """,
                (outbox_id, lease_token),
            )
            if updated.rowcount != 1:
                raise LearningOutboxLeaseError(outbox_id)

    def claim_reconciliation_worker(
        self,
        *,
        owner: LearningOwner,
        worker_id: str,
        lease_seconds: int = 30,
    ) -> LearningReconciliationWorkerLease | None:
        if not isinstance(owner, LearningOwner):
            raise TypeError("owner must be a LearningOwner")
        if not isinstance(worker_id, str) or not worker_id.strip() or len(worker_id) > 512:
            raise ValueError("worker_id must contain 1 to 512 characters")
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        token = f"learning-reconciler:{uuid4().hex}"
        with self._transaction() as conn:
            row = conn.execute(
                """
                INSERT INTO learning_reconciliation_workers(
                    owner_digest, worker_id, lease_token, lease_fence,
                    lease_expires_at, cursor_json, updated_at
                ) VALUES (
                    %s, %s, %s, 1,
                    CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'),
                    NULL, CURRENT_TIMESTAMP
                )
                ON CONFLICT (owner_digest) DO UPDATE SET
                    worker_id = EXCLUDED.worker_id,
                    lease_token = EXCLUDED.lease_token,
                    lease_fence = learning_reconciliation_workers.lease_fence + 1,
                    lease_expires_at = EXCLUDED.lease_expires_at,
                    updated_at = CURRENT_TIMESTAMP
                WHERE learning_reconciliation_workers.lease_expires_at
                    <= CURRENT_TIMESTAMP
                RETURNING lease_fence, lease_expires_at, cursor_json
                """,
                (owner.digest, worker_id, token, lease_seconds),
            ).fetchone()
        if row is None:
            return None
        cursor_json = row["cursor_json"]
        cursor = ReconciliationCursor.from_dict(json.loads(cursor_json)) if cursor_json else None
        return LearningReconciliationWorkerLease(
            owner_digest=owner.digest,
            worker_id=worker_id,
            lease_token=token,
            fence=int(row["lease_fence"]),
            lease_expires_at=_iso(row["lease_expires_at"]),
            cursor=cursor,
        )

    def checkpoint_reconciliation_worker(
        self,
        lease: LearningReconciliationWorkerLease,
        *,
        cursor: ReconciliationCursor | None,
        lease_seconds: int = 30,
    ) -> LearningReconciliationWorkerLease:
        if not isinstance(lease, LearningReconciliationWorkerLease):
            raise TypeError("lease must be a LearningReconciliationWorkerLease")
        if cursor is not None and cursor.owner_digest != lease.owner_digest:
            raise ValueError("cursor must be bound to the lease owner")
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        cursor_json = _canonical_json(cursor.to_dict()) if cursor is not None else None
        with self._transaction() as conn:
            row = conn.execute(
                """
                UPDATE learning_reconciliation_workers
                SET cursor_json = %s,
                    lease_expires_at = CURRENT_TIMESTAMP
                        + (%s * INTERVAL '1 second'),
                    updated_at = CURRENT_TIMESTAMP
                WHERE owner_digest = %s AND worker_id = %s AND lease_token = %s
                  AND lease_fence = %s AND lease_expires_at > CURRENT_TIMESTAMP
                RETURNING lease_expires_at
                """,
                (
                    cursor_json,
                    lease_seconds,
                    lease.owner_digest,
                    lease.worker_id,
                    lease.lease_token,
                    lease.fence,
                ),
            ).fetchone()
        if row is None:
            raise LearningReconciliationWorkerLeaseError()
        return replace(
            lease,
            lease_expires_at=_iso(row["lease_expires_at"]),
            cursor=cursor,
        )

    def release_reconciliation_worker(
        self,
        lease: LearningReconciliationWorkerLease,
    ) -> bool:
        if not isinstance(lease, LearningReconciliationWorkerLease):
            raise TypeError("lease must be a LearningReconciliationWorkerLease")
        with self._transaction() as conn:
            updated = conn.execute(
                """
                UPDATE learning_reconciliation_workers
                SET worker_id = NULL, lease_token = NULL,
                    lease_expires_at = '-infinity', updated_at = CURRENT_TIMESTAMP
                WHERE owner_digest = %s AND worker_id = %s AND lease_token = %s
                  AND lease_fence = %s AND lease_expires_at > CURRENT_TIMESTAMP
                """,
                (
                    lease.owner_digest,
                    lease.worker_id,
                    lease.lease_token,
                    lease.fence,
                ),
            )
        return updated.rowcount == 1


__all__ = [
    "LearningLedgerConnectionLostError",
    "LearningLedgerDependencyError",
    "LearningLedgerOverloadedError",
    "PostgresLearningLedger",
]
