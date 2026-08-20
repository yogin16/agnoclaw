"""Production-shaped PostgreSQL/service migration certification contracts."""

from __future__ import annotations

import json
import os
import tracemalloc
from uuid import uuid4

import pytest

from agnoclaw.migration_service import (
    PostgresMigrationBackupReceipt,
    PostgresMigrationDatabaseRef,
    load_postgres_schedule_map,
)
from agnoclaw.migration_service_apply import (
    apply_postgres_migration_012,
    rollback_postgres_migration_012,
    verify_postgres_migration_012,
)
from agnoclaw.migration_service_scan import (
    create_postgres_migration_012_plan_from_scan,
    scan_postgres_migration_012,
)
from agnoclaw.migration_service_transform import preview_postgres_migration_012_transforms


def _schedule_map(tmp_path):
    path = tmp_path / "production-schedule-map.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.1",
                "schedules": [
                    {
                        "source_schedule_id": "daily-review",
                        "schedule": "1h",
                        "prompt": "private production-matrix prompt",
                        "tenant_id": "tenant-production",
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
                        "concurrency_key": "tenant-production:daily-review",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return load_postgres_schedule_map(path)


def _create_source_tables(cursor, sql) -> None:
    cursor.execute(
        """
        CREATE TABLE agno.agno_learnings (
            learning_id text PRIMARY KEY,
            learning_type text NOT NULL,
            namespace text,
            user_id text,
            agent_id text,
            team_id text,
            workflow_id text,
            session_id text,
            entity_id text,
            entity_type text,
            content jsonb NOT NULL,
            metadata jsonb,
            created_at bigint NOT NULL,
            updated_at bigint
        )
        """
    )
    cursor.execute(
        "CREATE TABLE agno.agno_schedules (id text PRIMARY KEY, locked_by text, locked_at bigint)"
    )
    cursor.execute(
        "CREATE TABLE agno.agno_schedule_runs ("
        "id text PRIMARY KEY, schedule_id text NOT NULL, status text NOT NULL, "
        "completed_at bigint)"
    )
    cursor.execute(
        """
        INSERT INTO agno.agno_learnings(
            learning_id, learning_type, user_id, content, metadata, created_at, updated_at
        )
        SELECT
            'profile-' || source_id::text,
            'user_profile',
            'user-' || source_id::text,
            jsonb_build_object('preference', source_id),
            jsonb_build_object('source', 'production-matrix'),
            source_id,
            source_id
        FROM generate_series(1, 5000) AS source_id
        """
    )
    cursor.execute("INSERT INTO agno.agno_schedules(id) VALUES ('daily-review')")
    cursor.execute(
        "INSERT INTO agno.agno_schedule_runs(id, schedule_id, status, completed_at) "
        "VALUES ('run-1', 'daily-review', 'success', 5)"
    )


@pytest.mark.skipif(
    not os.getenv("AGNOCLAW_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL admin service",
)
def test_live_independent_databases_least_privilege_and_streaming_bounds(tmp_path) -> None:
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    admin_dsn = os.environ["AGNOCLAW_TEST_POSTGRES_URL"]
    suffix = uuid4().hex[:10]
    databases = {
        "source": f"acm012_src_{suffix}",
        "target_learning": f"acm012_lrn_{suffix}",
        "target_runtime": f"acm012_run_{suffix}",
    }
    roles = {
        "source": f"acm012_src_role_{suffix}",
        "target_learning": f"acm012_lrn_role_{suffix}",
        "target_runtime": f"acm012_run_role_{suffix}",
    }
    passwords = {
        role: f"Agnoclaw-qa-{suffix}-{index}!" for index, role in enumerate(roles, start=1)
    }

    def dsn_for(role: str, *, database_role: str | None = None) -> str:
        parameters = conninfo_to_dict(admin_dsn)
        selected_database = database_role or role
        parameters.update(
            dbname=databases[selected_database],
            user=roles[role],
            password=passwords[role],
        )
        return make_conninfo(**parameters)

    def admin_database_dsn(role: str) -> str:
        parameters = conninfo_to_dict(admin_dsn)
        parameters["dbname"] = databases[role]
        return make_conninfo(**parameters)

    admin = psycopg.connect(admin_dsn, autocommit=True)
    created_databases: list[str] = []
    created_roles: list[str] = []
    try:
        with admin.cursor() as cursor:
            for role in roles.values():
                cursor.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                        sql.Identifier(role),
                        sql.Literal(
                            passwords[next(key for key, value in roles.items() if value == role)]
                        ),
                    )
                )
                created_roles.append(role)
            for database in databases.values():
                cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
                created_databases.append(database)
                cursor.execute(
                    sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(
                        sql.Identifier(database)
                    )
                )
            for role_name, database in databases.items():
                cursor.execute(
                    sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                        sql.Identifier(database),
                        sql.Identifier(roles[role_name]),
                    )
                )

        for role_name in databases:
            with psycopg.connect(admin_database_dsn(role_name), autocommit=True) as setup:
                setup.execute("CREATE SCHEMA agno")
                setup.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
                setup.execute(
                    sql.SQL("GRANT USAGE ON SCHEMA agno TO {}").format(
                        sql.Identifier(roles[role_name])
                    )
                )
                if role_name == "source":
                    with setup.cursor() as cursor:
                        _create_source_tables(cursor, sql)
                    setup.execute(
                        sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA agno TO {}").format(
                            sql.Identifier(roles[role_name])
                        )
                    )
                else:
                    setup.execute(
                        sql.SQL("GRANT CREATE ON SCHEMA agno TO {}").format(
                            sql.Identifier(roles[role_name])
                        )
                    )

        with psycopg.connect(dsn_for("source"), autocommit=True) as source_reader:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                source_reader.execute(
                    "INSERT INTO agno.agno_schedules(id) VALUES ('not-authorized')"
                )
        with pytest.raises(psycopg.OperationalError, match="permission denied for database"):
            psycopg.connect(
                dsn_for("target_learning", database_role="target_runtime"),
                connect_timeout=3,
            )

        environment = {
            "AGNOCLAW_MATRIX_SOURCE_DSN": dsn_for("source"),
            "AGNOCLAW_MATRIX_LEARNING_DSN": dsn_for("target_learning"),
            "AGNOCLAW_MATRIX_RUNTIME_DSN": dsn_for("target_runtime"),
        }
        source = PostgresMigrationDatabaseRef("source", "AGNOCLAW_MATRIX_SOURCE_DSN", "agno")
        target_learning = PostgresMigrationDatabaseRef(
            "target_learning", "AGNOCLAW_MATRIX_LEARNING_DSN", "agno"
        )
        target_runtime = PostgresMigrationDatabaseRef(
            "target_runtime", "AGNOCLAW_MATRIX_RUNTIME_DSN", "agno"
        )
        schedule_map = _schedule_map(tmp_path)
        scan = scan_postgres_migration_012(
            source=source,
            target_learning=target_learning,
            target_runtime=target_runtime,
            schedule_map=schedule_map,
            environment=environment,
            max_rows_per_table=6000,
            batch_size=127,
        )
        assert scan.ready
        assert len(set(dict(scan.endpoint_evidence_digests).values())) == 3
        plan = create_postgres_migration_012_plan_from_scan(
            scan=scan,
            source=source,
            target_learning=target_learning,
            target_runtime=target_runtime,
            target_tenant_id="tenant-production",
            target_agent_id="reviewer",
            schedule_map=schedule_map,
            backup_receipt=PostgresMigrationBackupReceipt(
                receipt_id="independent-database-backup",
                receipt_digest="sha256:" + "6" * 64,
                restore_test_id="independent-database-restore",
            ),
            writer_fence_plan="deployment-fence:independent-databases:v1",
        )

        tracemalloc.start()
        preview = preview_postgres_migration_012_transforms(
            plan=plan,
            schedule_map=schedule_map,
            environment=environment,
            max_rows_per_table=6000,
            batch_size=127,
        )
        _, preview_peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert dict(preview.counts) == {
            "learning_personal": 5000,
            "learning_quarantine": 0,
            "schedule_history": 1,
            "schedule_job": 1,
        }
        assert preview_peak_bytes < 64 * 1024 * 1024
        assert "private production-matrix prompt" not in json.dumps(preview.to_dict())

        confirmations = {
            "plan": plan,
            "schedule_map": schedule_map,
            "confirm_plan_digest": plan.plan_digest,
            "confirm_transform_digest": preview.transform_digest,
            "confirm_writer_fence_plan": plan.writer_fence_plan,
            "writers_stopped": True,
            "environment": environment,
            "max_rows_per_table": 6000,
            "read_batch_size": 127,
        }
        applied = apply_postgres_migration_012(
            confirm_backup_receipt_digest=plan.backup_receipt.receipt_digest,
            write_batch_size=211,
            **confirmations,
        )
        assert sum(count for _, _, _, count in applied.counts) == 5002
        verified = verify_postgres_migration_012(**confirmations)
        assert set(dict(verified.role_phases).values()) == {"verified"}

        with psycopg.connect(admin_database_dsn("target_learning")) as target:
            assert target.execute("SELECT count(*) FROM agno.agno_learnings").fetchone()[0] == 5000
        with psycopg.connect(admin_database_dsn("target_runtime")) as target:
            assert (
                target.execute("SELECT count(*) FROM agno.runtime_scheduler_jobs").fetchone()[0]
                == 1
            )

        rolled_back = rollback_postgres_migration_012(
            write_batch_size=211,
            **confirmations,
        )
        assert set(dict(rolled_back.role_phases).values()) == {"rolled_back"}
    finally:
        tracemalloc.stop()
        with admin.cursor() as cursor:
            for database in reversed(created_databases):
                cursor.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        sql.Identifier(database)
                    )
                )
            for role in reversed(created_roles):
                cursor.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
        admin.close()
