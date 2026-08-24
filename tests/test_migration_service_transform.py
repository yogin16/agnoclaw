"""Deterministic PostgreSQL/service migration transformation contracts."""

from __future__ import annotations

import json
import os
from dataclasses import replace
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
from agnoclaw.migration_service_transform import (
    PostgresMigration012TransformReport,
    preview_postgres_migration_012_transforms,
)


def _schedule_map(tmp_path, *, include_schedule: bool = True):
    schedules = []
    if include_schedule:
        schedules.append(
            {
                "source_schedule_id": "daily-review",
                "schedule": "1h",
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
        )
    path = tmp_path / f"schedule-map-{len(schedules)}.json"
    path.write_text(
        json.dumps({"schema_version": "1.1", "schedules": schedules}),
        encoding="utf-8",
    )
    return load_postgres_schedule_map(path)


def _references(prefix: str):
    return (
        PostgresMigrationDatabaseRef("source", "AGNOCLAW_TRANSFORM_DSN", f"{prefix}_source"),
        PostgresMigrationDatabaseRef(
            "target_learning", "AGNOCLAW_TRANSFORM_DSN", f"{prefix}_learning"
        ),
        PostgresMigrationDatabaseRef(
            "target_runtime", "AGNOCLAW_TRANSFORM_DSN", f"{prefix}_runtime"
        ),
    )


def _backup_receipt() -> PostgresMigrationBackupReceipt:
    return PostgresMigrationBackupReceipt(
        receipt_id="backup-object-v7",
        receipt_digest="sha256:" + "5" * 64,
        restore_test_id="restore-drill-42",
    )


def _create_source_tables(cursor, sql, source_schema) -> None:
    schema = sql.Identifier(source_schema)
    cursor.execute(
        sql.SQL(
            "CREATE TABLE {}.agno_learnings ("
            "learning_id text PRIMARY KEY, learning_type text NOT NULL, "
            "namespace text, user_id text, agent_id text, team_id text, "
            "workflow_id text, session_id text, entity_id text, entity_type text, "
            "content jsonb NOT NULL, metadata jsonb, created_at bigint NOT NULL, "
            "updated_at bigint)"
        ).format(schema)
    )
    cursor.execute(
        sql.SQL(
            "CREATE TABLE {}.agno_schedules (id text PRIMARY KEY, locked_by text, locked_at bigint)"
        ).format(schema)
    )
    cursor.execute(
        sql.SQL(
            "CREATE TABLE {}.agno_schedule_runs ("
            "id text PRIMARY KEY, schedule_id text NOT NULL, status text NOT NULL, "
            "completed_at bigint)"
        ).format(schema)
    )


def test_transform_report_is_content_free_and_digest_bound() -> None:
    categories = tuple(
        sorted(
            (
                ("learning_personal", 3),
                ("learning_quarantine", 1),
                ("schedule_job", 1),
                ("schedule_history", 1),
            )
        )
    )
    report = PostgresMigration012TransformReport(
        migration_id="pgmig012_example",
        plan_digest="sha256:" + "1" * 64,
        generated_at="2026-08-14T00:00:00+00:00",
        counts=categories,
        category_digests=tuple((name, "sha256:" + "2" * 64) for name, _ in categories),
        source_table_digests=tuple(
            (table, "sha256:" + "3" * 64)
            for table in ("agno_learnings", "agno_schedules", "agno_schedule_runs")
        ),
        target_identity_digest="sha256:" + "4" * 64,
    )
    report = replace(report, transform_digest=report.computed_transform_digest)

    payload = json.dumps(report.to_dict(), sort_keys=True)
    assert report.transform_digest.startswith("sha256:")
    assert "private incident prompt" not in payload
    with pytest.raises(ValueError, match="transform_digest"):
        replace(
            report,
            counts=tuple((name, count + 1) for name, count in report.counts),
        )


@pytest.mark.skipif(
    not os.getenv("AGNOCLAW_TEST_POSTGRES_URL"),
    reason="requires the disposable PostgreSQL integration service",
)
def test_live_transform_preview_is_deterministic_bounded_and_read_only(tmp_path) -> None:
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql

    dsn = os.environ["AGNOCLAW_TEST_POSTGRES_URL"]
    prefix = "agnoclaw_transform_" + uuid4().hex[:12]
    source, target_learning, target_runtime = _references(prefix)
    schemas = (source.schema, target_learning.schema, target_runtime.schema)
    setup = psycopg.connect(dsn, autocommit=True)
    schedule_map = _schedule_map(tmp_path)
    mappings = (
        LegacyLearningScopeMapping(
            source_namespace="global",
            learning_type="learned_knowledge",
            action=LegacyScopeAction.QUARANTINE,
        ),
    )
    environment = {"AGNOCLAW_TRANSFORM_DSN": dsn}
    try:
        with setup.cursor() as cursor:
            for schema in schemas:
                cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            _create_source_tables(cursor, sql, source.schema)
            source_table = sql.SQL("{}.agno_learnings").format(sql.Identifier(source.schema))
            cursor.execute(
                sql.SQL(
                    "INSERT INTO {}(learning_id, learning_type, namespace, user_id, "
                    "session_id, content, metadata, created_at, updated_at) VALUES "
                    "(%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s), "
                    "(%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s), "
                    "(%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s), "
                    "(%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)"
                ).format(source_table),
                (
                    "profile-1",
                    "user_profile",
                    "personal",
                    "user-a",
                    None,
                    json.dumps({"name": "private-name"}),
                    json.dumps({"private": "metadata-secret"}),
                    1,
                    2,
                    "memory-1",
                    "user_memory",
                    "personal",
                    "user-a",
                    None,
                    json.dumps({"memories": ["private-memory"]}),
                    json.dumps({}),
                    1,
                    None,
                    "session-1",
                    "session_context",
                    "personal",
                    "user-a",
                    "session-a",
                    json.dumps({"summary": "private-session"}),
                    json.dumps({}),
                    1,
                    None,
                    "institutional-1",
                    "learned_knowledge",
                    "global",
                    None,
                    None,
                    json.dumps({"knowledge": "private-institutional"}),
                    json.dumps({}),
                    1,
                    None,
                ),
            )
            cursor.execute(
                sql.SQL("INSERT INTO {}.agno_schedules VALUES (%s, NULL, NULL)").format(
                    sql.Identifier(source.schema)
                ),
                ("daily-review",),
            )
            cursor.execute(
                sql.SQL("INSERT INTO {}.agno_schedule_runs VALUES (%s, %s, %s, %s)").format(
                    sql.Identifier(source.schema)
                ),
                ("run-1", "daily-review", "success", 1),
            )

        scan = scan_postgres_migration_012(
            source=source,
            target_learning=target_learning,
            target_runtime=target_runtime,
            schedule_map=schedule_map,
            scope_mappings=mappings,
            environment=environment,
            batch_size=1,
        )
        assert scan.ready
        plan = create_postgres_migration_012_plan_from_scan(
            scan=scan,
            source=source,
            target_learning=target_learning,
            target_runtime=target_runtime,
            target_tenant_id="tenant-a",
            target_agent_id="reviewer",
            schedule_map=schedule_map,
            backup_receipt=_backup_receipt(),
            writer_fence_plan="deployment-stop:v3",
            scope_mappings=mappings,
        )

        first = preview_postgres_migration_012_transforms(
            plan=plan,
            schedule_map=schedule_map,
            environment=environment,
            batch_size=1,
        )
        second = preview_postgres_migration_012_transforms(
            plan=plan,
            schedule_map=schedule_map,
            environment=environment,
            batch_size=2,
        )

        assert dict(first.counts) == {
            "learning_personal": 3,
            "learning_quarantine": 1,
            "schedule_history": 1,
            "schedule_job": 1,
        }
        assert first.transform_digest == second.transform_digest
        assert first.category_digests == second.category_digests
        serialized = json.dumps(first.to_dict(), sort_keys=True)
        for secret in (
            "private-name",
            "metadata-secret",
            "private-memory",
            "private-session",
            "private-institutional",
            "private incident prompt",
            dsn,
        ):
            assert secret not in serialized
        with setup.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema IN (%s, %s)",
                (target_learning.schema, target_runtime.schema),
            )
            assert cursor.fetchone()[0] == 0
            cursor.execute(
                sql.SQL("UPDATE {} SET content = %s::jsonb WHERE learning_id = %s").format(
                    source_table
                ),
                (json.dumps({"name": "changed-after-plan"}), "profile-1"),
            )
        with pytest.raises(Migration012Error) as drift:
            preview_postgres_migration_012_transforms(
                plan=plan,
                schedule_map=schedule_map,
                environment=environment,
            )
        assert drift.value.code == "MIGRATION_POSTGRES_PLAN_EVIDENCE_DRIFT"
    finally:
        with setup.cursor() as cursor:
            for schema in reversed(schemas):
                cursor.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
                )
        setup.close()


@pytest.mark.skipif(
    not os.getenv("AGNOCLAW_TEST_POSTGRES_URL"),
    reason="requires the disposable PostgreSQL integration service",
)
def test_live_scan_blocks_personal_rekey_collisions_before_planning(tmp_path) -> None:
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql

    dsn = os.environ["AGNOCLAW_TEST_POSTGRES_URL"]
    prefix = "agnoclaw_transform_collision_" + uuid4().hex[:12]
    source, target_learning, target_runtime = _references(prefix)
    schemas = (source.schema, target_learning.schema, target_runtime.schema)
    setup = psycopg.connect(dsn, autocommit=True)
    try:
        with setup.cursor() as cursor:
            for schema in schemas:
                cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            _create_source_tables(cursor, sql, source.schema)
            cursor.execute(
                sql.SQL(
                    "INSERT INTO {}.agno_learnings(learning_id, learning_type, user_id, "
                    "content, created_at) VALUES "
                    "(%s, 'user_profile', %s, %s::jsonb, 1), "
                    "(%s, 'user_profile', %s, %s::jsonb, 1)"
                ).format(sql.Identifier(source.schema)),
                (
                    "profile-1",
                    "same-owner",
                    json.dumps({"version": 1}),
                    "profile-2",
                    "same-owner",
                    json.dumps({"version": 2}),
                ),
            )
        report = scan_postgres_migration_012(
            source=source,
            target_learning=target_learning,
            target_runtime=target_runtime,
            schedule_map=_schedule_map(tmp_path, include_schedule=False),
            environment={"AGNOCLAW_TRANSFORM_DSN": dsn},
        )

        assert not report.ready
        collision = next(
            item
            for item in report.findings
            if item.code == "MIGRATION_POSTGRES_LEARNING_TARGET_COLLISION"
        )
        assert collision.count == 1
    finally:
        with setup.cursor() as cursor:
            for schema in reversed(schemas):
                cursor.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
                )
        setup.close()
