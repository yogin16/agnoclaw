"""PostgreSQL/service migration mutation lifecycle contracts."""

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
from agnoclaw.migration_service_apply import (
    PostgresMigration012LifecycleReceipt,
    apply_postgres_migration_012,
    cutover_postgres_migration_012,
    rollback_postgres_migration_012,
    verify_postgres_migration_012,
)
from agnoclaw.migration_service_scan import (
    create_postgres_migration_012_plan_from_scan,
    scan_postgres_migration_012,
)
from agnoclaw.migration_service_transform import preview_postgres_migration_012_transforms


def _schedule_map(tmp_path):
    path = tmp_path / "schedule-map.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.1",
                "schedules": [
                    {
                        "source_schedule_id": "daily-review",
                        "schedule": "1h",
                        "prompt": "private lifecycle prompt",
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


def _backup_receipt() -> PostgresMigrationBackupReceipt:
    return PostgresMigrationBackupReceipt(
        receipt_id="backup-object-v7",
        receipt_digest="sha256:" + "5" * 64,
        restore_test_id="restore-drill-42",
    )


def _references(prefix: str):
    return (
        PostgresMigrationDatabaseRef("source", "AGNOCLAW_APPLY_DSN", f"{prefix}_source"),
        PostgresMigrationDatabaseRef("target_learning", "AGNOCLAW_APPLY_DSN", f"{prefix}_learning"),
        PostgresMigrationDatabaseRef("target_runtime", "AGNOCLAW_APPLY_DSN", f"{prefix}_runtime"),
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


def _crash_apply_worker(plan, schedule_map, environment, transform_digest, stage: str) -> None:
    def crash_at_checkpoint(operation, observed_stage, _roles, checkpoint) -> None:
        if operation == "apply" and observed_stage == stage and checkpoint == 1:
            os._exit(91)

    apply_postgres_migration_012(
        plan=plan,
        schedule_map=schedule_map,
        confirm_plan_digest=plan.plan_digest,
        confirm_transform_digest=transform_digest,
        confirm_backup_receipt_digest=plan.backup_receipt.receipt_digest,
        confirm_writer_fence_plan=plan.writer_fence_plan,
        writers_stopped=True,
        environment=environment,
        read_batch_size=1,
        write_batch_size=1,
        _checkpoint_observer=crash_at_checkpoint,
    )


def _crash_rollback_worker(plan, schedule_map, environment, transform_digest) -> None:
    def crash_after_checkpoint(operation, stage, _roles, checkpoint) -> None:
        if operation == "rollback" and stage == "after_batch_commit" and checkpoint == 1:
            os._exit(92)

    rollback_postgres_migration_012(
        plan=plan,
        schedule_map=schedule_map,
        confirm_plan_digest=plan.plan_digest,
        confirm_transform_digest=transform_digest,
        confirm_writer_fence_plan=plan.writer_fence_plan,
        writers_stopped=True,
        confirm_no_post_cutover_target_writes=True,
        environment=environment,
        read_batch_size=1,
        write_batch_size=1,
        _checkpoint_observer=crash_after_checkpoint,
    )


def test_lifecycle_receipt_is_content_free_and_digest_bound() -> None:
    receipt = PostgresMigration012LifecycleReceipt(
        operation="apply",
        migration_id="pgmig012_example",
        plan_digest="sha256:" + "1" * 64,
        transform_digest="sha256:" + "2" * 64,
        generated_at="2026-08-14T00:00:00+00:00",
        role_phases=(("target_learning", "applied"), ("target_runtime", "applied")),
        counts=(("target_learning", "learning_personal", "inserted", 1),),
        rollback_available=True,
    )
    receipt = replace(receipt, receipt_digest=receipt.computed_receipt_digest)

    payload = json.dumps(receipt.to_dict(), sort_keys=True)
    assert receipt.receipt_digest.startswith("sha256:")
    assert "private lifecycle prompt" not in payload
    with pytest.raises(ValueError, match="receipt_digest"):
        replace(receipt, rollback_available=False)


@pytest.mark.skipif(
    not os.getenv("AGNOCLAW_TEST_POSTGRES_URL"),
    reason="requires the disposable PostgreSQL integration service",
)
def test_live_service_apply_verify_cutover_and_rollback(tmp_path) -> None:
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql

    dsn = os.environ["AGNOCLAW_TEST_POSTGRES_URL"]
    prefix = "agnoclaw_apply_" + uuid4().hex[:12]
    source, target_learning, target_runtime = _references(prefix)
    schemas = (source.schema, target_learning.schema, target_runtime.schema)
    environment = {"AGNOCLAW_APPLY_DSN": dsn}
    schedule_map = _schedule_map(tmp_path)
    mappings = (
        LegacyLearningScopeMapping(
            source_namespace="global",
            learning_type="learned_knowledge",
            action=LegacyScopeAction.QUARANTINE,
        ),
    )
    setup = psycopg.connect(dsn, autocommit=True)
    try:
        with setup.cursor() as cursor:
            for schema in schemas:
                cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            _create_source_tables(cursor, sql, source.schema)
            learning = sql.SQL("{}.agno_learnings").format(sql.Identifier(source.schema))
            cursor.execute(
                sql.SQL(
                    "INSERT INTO {}(learning_id, learning_type, namespace, user_id, "
                    "content, metadata, created_at, updated_at) VALUES "
                    "(%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s), "
                    "(%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)"
                ).format(learning),
                (
                    "legacy-profile",
                    "user_profile",
                    None,
                    "user-a",
                    '{"name":"Private User"}',
                    '{"source":"legacy"}',
                    1,
                    2,
                    "legacy-knowledge",
                    "learned_knowledge",
                    "global",
                    None,
                    '{"secret":"institutional"}',
                    "{}",
                    3,
                    4,
                ),
            )
            schedules = sql.SQL("{}.agno_schedules").format(sql.Identifier(source.schema))
            cursor.execute(
                sql.SQL("INSERT INTO {}(id) VALUES (%s)").format(schedules),
                ("daily-review",),
            )
            runs = sql.SQL("{}.agno_schedule_runs").format(sql.Identifier(source.schema))
            cursor.execute(
                sql.SQL(
                    "INSERT INTO {}(id, schedule_id, status, completed_at) VALUES (%s, %s, %s, %s)"
                ).format(runs),
                ("run-1", "daily-review", "success", 5),
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
        preview = preview_postgres_migration_012_transforms(
            plan=plan,
            schedule_map=schedule_map,
            environment=environment,
            batch_size=1,
        )
        confirmations = {
            "confirm_plan_digest": plan.plan_digest,
            "confirm_transform_digest": preview.transform_digest,
            "confirm_writer_fence_plan": plan.writer_fence_plan,
            "writers_stopped": True,
            "environment": environment,
            "read_batch_size": 1,
        }

        schema_lock_key = f"agnoclaw:migration:0.12:{target_learning.schema}"
        setup.execute(
            "SELECT pg_catalog.pg_advisory_lock(pg_catalog.hashtextextended(%s, 0))",
            (schema_lock_key,),
        )
        try:
            with pytest.raises(Migration012Error, match="Another migration owns"):
                apply_postgres_migration_012(
                    plan=plan,
                    schedule_map=schedule_map,
                    confirm_backup_receipt_digest=plan.backup_receipt.receipt_digest,
                    write_batch_size=1,
                    **confirmations,
                )
        finally:
            setup.execute(
                "SELECT pg_catalog.pg_advisory_unlock(pg_catalog.hashtextextended(%s, 0))",
                (schema_lock_key,),
            )

        import multiprocessing

        context = multiprocessing.get_context("spawn")
        for stage in (
            "after_initialize_commit",
            "before_batch_commit",
            "after_batch_commit",
        ):
            process = context.Process(
                target=_crash_apply_worker,
                args=(plan, schedule_map, environment, preview.transform_digest, stage),
            )
            process.start()
            process.join(30)
            if process.is_alive():
                process.terminate()
                process.join(5)
                pytest.fail(f"crash worker did not terminate at {stage}")
            assert process.exitcode == 91
        applied = apply_postgres_migration_012(
            plan=plan,
            schedule_map=schedule_map,
            confirm_backup_receipt_digest=plan.backup_receipt.receipt_digest,
            write_batch_size=1,
            **confirmations,
        )
        assert dict(applied.role_phases) == {
            "target_learning": "applied",
            "target_runtime": "applied",
        }
        assert sum(item[3] for item in applied.counts) == 4
        assert "Private User" not in json.dumps(applied.to_dict())
        assert "private lifecycle prompt" not in json.dumps(applied.to_dict())

        repeated = apply_postgres_migration_012(
            plan=plan,
            schedule_map=schedule_map,
            confirm_backup_receipt_digest=plan.backup_receipt.receipt_digest,
            write_batch_size=1,
            **confirmations,
        )
        assert repeated.receipt_digest == applied.receipt_digest

        verification_dry_run = verify_postgres_migration_012(
            plan=plan,
            schedule_map=schedule_map,
            dry_run=True,
            **confirmations,
        )
        assert set(dict(verification_dry_run.role_phases).values()) == {"applied"}

        verified = verify_postgres_migration_012(
            plan=plan,
            schedule_map=schedule_map,
            **confirmations,
        )
        assert set(dict(verified.role_phases).values()) == {"verified"}

        with setup.cursor() as cursor:
            learning_target = sql.SQL("{}.agno_learnings").format(
                sql.Identifier(target_learning.schema)
            )
            cursor.execute(
                sql.SQL(
                    "INSERT INTO {}(learning_id, learning_type, content, created_at) "
                    "VALUES (%s, %s, %s::jsonb, %s)"
                ).format(learning_target),
                ("rogue-target-row", "user_profile", "{}", 99),
            )
        with pytest.raises(Migration012Error, match="outside migration ownership"):
            verify_postgres_migration_012(
                plan=plan,
                schedule_map=schedule_map,
                **confirmations,
            )
        with setup.cursor() as cursor:
            cursor.execute(
                sql.SQL("DELETE FROM {}.agno_learnings WHERE learning_id = %s").format(
                    sql.Identifier(target_learning.schema)
                ),
                ("rogue-target-row",),
            )

        cutover_dry_run = cutover_postgres_migration_012(
            plan=plan,
            schedule_map=schedule_map,
            cutover_receipt_id="deployment-change-42",
            cutover_receipt_digest="sha256:" + "8" * 64,
            dry_run=True,
            **confirmations,
        )
        assert set(dict(cutover_dry_run.role_phases).values()) == {"verified"}

        cutover = cutover_postgres_migration_012(
            plan=plan,
            schedule_map=schedule_map,
            cutover_receipt_id="deployment-change-42",
            cutover_receipt_digest="sha256:" + "8" * 64,
            **confirmations,
        )
        assert set(dict(cutover.role_phases).values()) == {"cutover"}
        rollback_dry_run = rollback_postgres_migration_012(
            plan=plan,
            schedule_map=schedule_map,
            write_batch_size=1,
            confirm_no_post_cutover_target_writes=True,
            dry_run=True,
            **confirmations,
        )
        assert set(dict(rollback_dry_run.role_phases).values()) == {"cutover"}
        with pytest.raises(
            Migration012Error,
            match="Post-cutover rollback requires confirmation",
        ):
            rollback_postgres_migration_012(
                plan=plan,
                schedule_map=schedule_map,
                write_batch_size=1,
                **confirmations,
            )

        with setup.cursor() as cursor:
            jobs = sql.SQL("{}.runtime_scheduler_jobs").format(
                sql.Identifier(target_runtime.schema)
            )
            cursor.execute(sql.SQL("SELECT job_name, job_json FROM {}").format(jobs))
            job_name, original_job_json = cursor.fetchone()
            cursor.execute(
                sql.SQL("UPDATE {} SET job_json = %s WHERE job_name = %s").format(jobs),
                (json.dumps({"drift": True}), job_name),
            )
        with pytest.raises(Migration012Error, match="missing or changed migration-owned"):
            rollback_postgres_migration_012(
                plan=plan,
                schedule_map=schedule_map,
                write_batch_size=1,
                confirm_no_post_cutover_target_writes=True,
                **confirmations,
            )
        with setup.cursor() as cursor:
            cursor.execute(
                sql.SQL("UPDATE {} SET job_json = %s WHERE job_name = %s").format(jobs),
                (original_job_json, job_name),
            )

        rollback_process = context.Process(
            target=_crash_rollback_worker,
            args=(plan, schedule_map, environment, preview.transform_digest),
        )
        rollback_process.start()
        rollback_process.join(30)
        if rollback_process.is_alive():
            rollback_process.terminate()
            rollback_process.join(5)
            pytest.fail("rollback crash worker did not terminate")
        assert rollback_process.exitcode == 92

        rolled_back = rollback_postgres_migration_012(
            plan=plan,
            schedule_map=schedule_map,
            write_batch_size=1,
            confirm_no_post_cutover_target_writes=True,
            **confirmations,
        )
        assert set(dict(rolled_back.role_phases).values()) == {"rolled_back"}
        assert rolled_back.rollback_available is False

        with setup.cursor() as cursor:
            for schema, table in (
                (target_learning.schema, "agno_learnings"),
                (target_learning.schema, "agnoclaw_migration_012_learning_quarantine"),
                (target_runtime.schema, "runtime_scheduler_jobs"),
                (target_runtime.schema, "agnoclaw_migration_012_schedule_history"),
            ):
                qualified = sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(table))
                cursor.execute(sql.SQL("SELECT count(*) FROM {}").format(qualified))
                assert cursor.fetchone()[0] == 0
            for schema in (target_learning.schema, target_runtime.schema):
                provenance = sql.SQL("{}.agnoclaw_migration_012_provenance").format(
                    sql.Identifier(schema)
                )
                cursor.execute(
                    sql.SQL(
                        "SELECT count(*), count(rolled_back_at) FROM {} WHERE migration_id = %s"
                    ).format(provenance),
                    (plan.migration_id,),
                )
                total, rolled = cursor.fetchone()
                assert total == rolled

        repeated_rollback = rollback_postgres_migration_012(
            plan=plan,
            schedule_map=schedule_map,
            write_batch_size=1,
            confirm_no_post_cutover_target_writes=True,
            **confirmations,
        )
        assert repeated_rollback.receipt_digest == rolled_back.receipt_digest
    finally:
        with setup.cursor() as cursor:
            for schema in reversed(schemas):
                cursor.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
                )
        setup.close()
