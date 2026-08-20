"""Read-only PostgreSQL/service migration scanner contracts."""

from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest

from agnoclaw.migration import LegacyLearningScopeMapping, LegacyScopeAction
from agnoclaw.migration_apply import Migration012Error
from agnoclaw.migration_service import (
    PostgresMigrationBackupReceipt,
    PostgresMigrationDatabaseRef,
    load_postgres_schedule_map,
)
from agnoclaw.migration_service_scan import (
    create_postgres_migration_012_plan_from_scan,
    scan_postgres_migration_012,
)


def _schedule_map(tmp_path, *, source_schedule_id: str = "daily-review"):
    path = tmp_path / f"schedule-map-{source_schedule_id}.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.1",
                "schedules": [
                    {
                        "source_schedule_id": source_schedule_id,
                        "schedule": "0 9 * * *",
                        "prompt": "private incident prompt",
                        "tenant_id": "tenant-a",
                        "user_id": "scheduler",
                        "session_id": "schedule-daily-review",
                        "agent_id": "reviewer",
                        "worker_profile": "service-reviewer-v3",
                        "enabled": True,
                        "isolated": True,
                        "learning_consent": False,
                        "timezone": "Asia/Dubai",
                        "misfire_policy": "skip",
                        "misfire_grace_seconds": 300,
                        "overlap_policy": "skip",
                        "max_retries": 3,
                        "retry_delay_seconds": 30,
                        "retry_backoff_multiplier": 2.0,
                        "retry_max_delay_seconds": 3600,
                        "retry_jitter_seconds": 0,
                        "jitter_seconds": 0,
                        "concurrency_key": "tenant-a:daily-review",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return load_postgres_schedule_map(path)


def _references(prefix: str):
    return (
        PostgresMigrationDatabaseRef("source", "AGNOCLAW_SCAN_DSN", f"{prefix}_source"),
        PostgresMigrationDatabaseRef("target_learning", "AGNOCLAW_SCAN_DSN", f"{prefix}_learning"),
        PostgresMigrationDatabaseRef("target_runtime", "AGNOCLAW_SCAN_DSN", f"{prefix}_runtime"),
    )


def test_scan_connection_errors_are_stable_and_secret_free(tmp_path) -> None:
    pytest.importorskip("psycopg")
    source, target_learning, target_runtime = _references("scan_error")
    secret_dsn = "postgresql://private_user:supersecret@127.0.0.1:1/private_db"

    with pytest.raises(Migration012Error) as failure:
        scan_postgres_migration_012(
            source=source,
            target_learning=target_learning,
            target_runtime=target_runtime,
            schedule_map=_schedule_map(tmp_path),
            environment={"AGNOCLAW_SCAN_DSN": secret_dsn},
        )

    assert failure.value.code == "MIGRATION_POSTGRES_SCAN_FAILED"
    assert "supersecret" not in str(failure.value)
    assert secret_dsn not in str(failure.value)


def test_scan_bounds_fail_before_resolving_credentials(tmp_path) -> None:
    source, target_learning, target_runtime = _references("scan_bounds")
    with pytest.raises(ValueError, match="batch_size"):
        scan_postgres_migration_012(
            source=source,
            target_learning=target_learning,
            target_runtime=target_runtime,
            schedule_map=_schedule_map(tmp_path),
            environment={},
            batch_size=0,
        )


@pytest.mark.skipif(
    not os.getenv("AGNOCLAW_TEST_POSTGRES_URL"),
    reason="requires the disposable PostgreSQL integration service",
)
def test_live_scan_is_read_only_streamed_and_content_free(tmp_path) -> None:
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql

    dsn = os.environ["AGNOCLAW_TEST_POSTGRES_URL"]
    prefix = "agnoclaw_migration_scan_" + uuid4().hex[:12]
    source, target_learning, target_runtime = _references(prefix)
    schemas = (source.schema, target_learning.schema, target_runtime.schema)
    setup = psycopg.connect(dsn, autocommit=True)
    try:
        with setup.cursor() as cursor:
            for schema in schemas:
                cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            source_schema = sql.Identifier(source.schema)
            cursor.execute(
                sql.SQL(
                    "CREATE TABLE {}.agno_learnings ("
                    "learning_id text PRIMARY KEY, learning_type text NOT NULL, "
                    "namespace text, user_id text, agent_id text, team_id text, "
                    "workflow_id text, session_id text, entity_id text, entity_type text, "
                    "content jsonb NOT NULL, metadata jsonb, created_at bigint NOT NULL, "
                    "updated_at bigint)"
                ).format(source_schema)
            )
            cursor.execute(
                sql.SQL(
                    "CREATE TABLE {}.agno_schedules ("
                    "id text PRIMARY KEY, locked_by text, locked_at bigint)"
                ).format(source_schema)
            )
            cursor.execute(
                sql.SQL(
                    "CREATE TABLE {}.agno_schedule_runs ("
                    "id text PRIMARY KEY, schedule_id text NOT NULL, "
                    "status text NOT NULL, completed_at bigint)"
                ).format(source_schema)
            )
            cursor.execute(
                sql.SQL(
                    "INSERT INTO {}.agno_learnings "
                    "(learning_id, learning_type, namespace, user_id, session_id, "
                    "content, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s), "
                    "(%s, %s, %s, %s, %s, %s::jsonb, %s)"
                ).format(source_schema),
                (
                    "personal-1",
                    "user_profile",
                    "personal",
                    "user-a",
                    None,
                    json.dumps({"private": "private-learning-secret"}),
                    1,
                    "institutional-1",
                    "learned_knowledge",
                    "global",
                    None,
                    None,
                    json.dumps({"private": "institutional-secret"}),
                    1,
                ),
            )
            cursor.execute(
                sql.SQL("INSERT INTO {}.agno_schedules VALUES (%s, NULL, NULL)").format(
                    source_schema
                ),
                ("daily-review",),
            )
            cursor.execute(
                sql.SQL("INSERT INTO {}.agno_schedule_runs VALUES (%s, %s, %s, %s)").format(
                    source_schema
                ),
                ("run-1", "daily-review", "success", 1),
            )

        schedule_map = _schedule_map(tmp_path)
        scope_mappings = (
            LegacyLearningScopeMapping(
                source_namespace="global",
                learning_type="learned_knowledge",
                action=LegacyScopeAction.QUARANTINE,
            ),
        )
        report = scan_postgres_migration_012(
            source=source,
            target_learning=target_learning,
            target_runtime=target_runtime,
            schedule_map=schedule_map,
            scope_mappings=scope_mappings,
            environment={"AGNOCLAW_SCAN_DSN": dsn},
            batch_size=1,
        )

        serialized = json.dumps(report.to_dict(), sort_keys=True)
        assert report.ready
        assert len(report.endpoint_evidence_digests) == 3
        assert len(report.table_evidence) == 12
        assert not report.findings
        assert "private-learning-secret" not in serialized
        assert "institutional-secret" not in serialized
        assert "private incident prompt" not in serialized
        assert dsn not in serialized
        source_learning = next(
            item
            for item in report.table_evidence
            if item.role == "source" and item.table_name == "agno_learnings"
        )
        assert source_learning.exists
        assert source_learning.row_count == 2
        assert source_learning.logical_digest is not None
        plan = create_postgres_migration_012_plan_from_scan(
            scan=report,
            source=source,
            target_learning=target_learning,
            target_runtime=target_runtime,
            target_tenant_id="tenant-a",
            target_agent_id="reviewer",
            schedule_map=schedule_map,
            backup_receipt=PostgresMigrationBackupReceipt(
                receipt_id="backup-object-v7",
                receipt_digest="sha256:" + "5" * 64,
                restore_test_id="restore-drill-42",
            ),
            writer_fence_plan="deployment-stop:v3",
            scope_mappings=scope_mappings,
        )
        assert plan.endpoint_evidence_digests == report.endpoint_evidence_digests
        with pytest.raises(Migration012Error) as changed_scope:
            create_postgres_migration_012_plan_from_scan(
                scan=report,
                source=source,
                target_learning=target_learning,
                target_runtime=target_runtime,
                target_tenant_id="tenant-a",
                target_agent_id="reviewer",
                schedule_map=schedule_map,
                backup_receipt=plan.backup_receipt,
                writer_fence_plan="deployment-stop:v3",
                scope_mappings=(),
            )
        assert changed_scope.value.code == "MIGRATION_POSTGRES_SCAN_SCOPE_MISMATCH"
        with setup.cursor() as cursor:
            cursor.execute(
                sql.SQL("INSERT INTO {}.agno_schedule_runs VALUES (%s, %s, %s, %s)").format(
                    source_schema
                ),
                ("run-orphan", "missing-schedule", "success", 2),
            )
        orphaned = scan_postgres_migration_012(
            source=source,
            target_learning=target_learning,
            target_runtime=target_runtime,
            schedule_map=schedule_map,
            scope_mappings=scope_mappings,
            environment={"AGNOCLAW_SCAN_DSN": dsn},
            batch_size=1,
        )
        assert not orphaned.ready
        assert "MIGRATION_POSTGRES_SCHEDULE_RUN_ORPHANED" in {
            finding.code for finding in orphaned.findings
        }
    finally:
        with setup.cursor() as cursor:
            for schema in reversed(schemas):
                cursor.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
                )
        setup.close()
