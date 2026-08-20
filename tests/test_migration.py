"""Read-only 0.12 migration preflight contracts."""

from __future__ import annotations

import json
import sqlite3

import pytest
from click.testing import CliRunner

from agnoclaw.cli.main import cli
from agnoclaw.migration import (
    LegacyLearningScopeMapping,
    LegacyScopeAction,
    MigrationSeverity,
    inspect_migration_012,
)


def _learning_db(path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE agno_learnings (
            learning_id TEXT PRIMARY KEY,
            learning_type TEXT NOT NULL,
            namespace TEXT,
            user_id TEXT,
            agent_id TEXT,
            team_id TEXT,
            workflow_id TEXT,
            session_id TEXT,
            entity_id TEXT,
            entity_type TEXT,
            content TEXT,
            metadata TEXT,
            created_at INTEGER,
            updated_at INTEGER
        );
        """
    )
    connection.commit()
    connection.close()


def _insert_learning(
    path,
    *,
    learning_id: str,
    learning_type: str,
    namespace: str | None,
    user_id: str | None = None,
    session_id: str | None = None,
    secret: str = "sensitive-content",
) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        """
        INSERT INTO agno_learnings(
            learning_id, learning_type, namespace, user_id, session_id, content
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (learning_id, learning_type, namespace, user_id, session_id, secret),
    )
    connection.commit()
    connection.close()


def _codes(report) -> set[str]:
    return {finding.code for finding in report.findings}


def test_learning_preflight_is_deterministic_content_safe_and_read_only(tmp_path) -> None:
    path = tmp_path / "learning.db"
    _learning_db(path)
    _insert_learning(
        path,
        learning_id="profile-1",
        learning_type="user_profile",
        namespace="user",
        user_id="legacy-user",
    )
    _insert_learning(
        path,
        learning_id="knowledge-1",
        learning_type="learned_knowledge",
        namespace="global",
    )
    before = path.stat().st_mtime_ns
    mapping = LegacyLearningScopeMapping(
        source_namespace="global",
        learning_type="learned_knowledge",
        action=LegacyScopeAction.QUARANTINE,
    )

    first = inspect_migration_012(
        learning_sqlite_path=path,
        scope_mappings=(mapping,),
    )
    second = inspect_migration_012(
        learning_sqlite_path=path,
        scope_mappings=(mapping,),
    )

    assert first.preflight_clear is True
    assert first.apply_allowed is False
    assert first.read_only is True
    assert first.report_digest == second.report_digest
    assert first.learning_tables[0].row_count == 2
    assert first.learning_tables[0].logical_digest is not None
    assert "sensitive-content" not in json.dumps(first.to_dict())
    assert path.stat().st_mtime_ns == before


def test_learning_preflight_reads_a_real_agno_sqlite_learning_table(tmp_path) -> None:
    from agno.db.sqlite import SqliteDb

    path = tmp_path / "agno.db"
    database = SqliteDb(db_file=str(path))
    database.upsert_learning(
        id="legacy-learning-1",
        learning_type="learned_knowledge",
        namespace="global",
        content={"learning": "sensitive real Agno learning"},
    )
    database.close()
    mapping = LegacyLearningScopeMapping(
        source_namespace="global",
        learning_type="learned_knowledge",
        action=LegacyScopeAction.QUARANTINE,
    )

    report = inspect_migration_012(
        learning_sqlite_path=path,
        scope_mappings=(mapping,),
    )

    assert report.preflight_clear is True
    assert report.learning_tables[0].shape == "agno_unified_learning"
    assert report.learning_tables[0].row_count == 1
    assert "sensitive real Agno learning" not in json.dumps(report.to_dict())


def test_learning_preflight_blocks_unresolved_scope_owner_collision_and_limit(
    tmp_path,
) -> None:
    path = tmp_path / "learning.db"
    _learning_db(path)
    _insert_learning(
        path,
        learning_id="knowledge-1",
        learning_type="learned_knowledge",
        namespace="global",
    )
    _insert_learning(
        path,
        learning_id="knowledge-2",
        learning_type="learned_knowledge",
        namespace="global",
    )
    _insert_learning(
        path,
        learning_id="profile-missing-owner",
        learning_type="user_profile",
        namespace="user",
    )

    report = inspect_migration_012(learning_sqlite_path=path)

    assert report.preflight_clear is False
    assert {
        "MIGRATION_LEARNING_SCOPE_UNRESOLVED",
        "MIGRATION_LEARNING_OWNER_MISSING",
        "MIGRATION_LEARNING_SCOPE_COLLISION",
    } <= _codes(report)
    assert all(
        finding.severity is MigrationSeverity.BLOCKER
        for finding in report.findings
        if finding.code.startswith("MIGRATION_LEARNING_")
    )

    bounded = inspect_migration_012(
        learning_sqlite_path=path,
        max_learning_rows=2,
    )
    assert "MIGRATION_LEARNING_SCAN_LIMIT_EXCEEDED" in _codes(bounded)
    assert bounded.learning_tables[0].scanned_rows == 0

    bounded_bytes = inspect_migration_012(
        learning_sqlite_path=path,
        max_learning_bytes=1,
    )
    assert "MIGRATION_LEARNING_SOURCE_TOO_LARGE" in _codes(bounded_bytes)


def test_legacy_memory_table_detects_rows_without_any_owner(tmp_path) -> None:
    path = tmp_path / "memory.db"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE agno_memories (
            memory_id TEXT PRIMARY KEY,
            memory TEXT,
            user_id TEXT,
            agent_id TEXT,
            team_id TEXT
        )
        """
    )
    connection.execute(
        "INSERT INTO agno_memories(memory_id, memory) VALUES (?, ?)",
        ("memory-1", "private memory"),
    )
    connection.commit()
    connection.close()

    report = inspect_migration_012(learning_sqlite_path=path)

    assert "MIGRATION_LEARNING_OWNER_MISSING" in _codes(report)
    assert "private memory" not in json.dumps(report.to_dict())


def test_schedule_preflight_requires_explicit_semantics_and_fence(tmp_path) -> None:
    path = tmp_path / "schedules.json"
    path.write_text(
        json.dumps(
            {
                "jobs": [
                    {"name": "daily", "schedule": "0 8 * * *", "prompt": "one"},
                    {"name": "daily", "schedule": "1h", "prompt": "two"},
                ],
                "runs": [
                    {
                        "run_id": "run-1",
                        "job_name": "missing",
                        "status": "running",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    report = inspect_migration_012(schedule_json_path=path)

    assert report.preflight_clear is False
    assert {
        "MIGRATION_SCHEDULE_JOB_COLLISION",
        "MIGRATION_SCHEDULE_TIMEZONE_UNRESOLVED",
        "MIGRATION_SCHEDULE_MISFIRE_UNRESOLVED",
        "MIGRATION_SCHEDULE_RUN_ORPHANED",
        "MIGRATION_SCHEDULE_RUN_IN_FLIGHT",
        "MIGRATION_SCHEDULE_FENCE_UNPLANNED",
    } <= _codes(report)


def test_schedule_preflight_accepts_complete_plan_but_never_allows_apply(tmp_path) -> None:
    path = tmp_path / "schedules.json"
    path.write_text(
        json.dumps(
            {
                "format_version": "legacy-v1",
                "jobs": [
                    {
                        "name": "daily",
                        "schedule": "0 8 * * *",
                        "prompt": "report",
                        "enabled": True,
                    }
                ],
                "runs": [],
            }
        ),
        encoding="utf-8",
    )

    report = inspect_migration_012(
        schedule_json_path=path,
        schedule_default_timezone="Asia/Dubai",
        schedule_default_misfire_policy="skip",
        old_writer_fence_plan="stop-service-and-record-fence:v1",
    )

    assert report.preflight_clear is True
    assert report.apply_allowed is False
    assert report.schedule is not None
    assert report.schedule.format_version == "legacy-v1"
    assert report.schedule.job_count == 1


def test_schedule_preflight_blocks_per_job_model_partition(tmp_path) -> None:
    path = tmp_path / "schedules.json"
    path.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "name": "specialized",
                        "schedule": "1h",
                        "prompt": "report",
                        "model_id": "legacy-model",
                    }
                ],
                "runs": [],
            }
        ),
        encoding="utf-8",
    )

    report = inspect_migration_012(
        schedule_json_path=path,
        schedule_default_timezone="UTC",
        schedule_default_misfire_policy="skip",
        old_writer_fence_plan="stop-service:v1",
    )

    assert "MIGRATION_SCHEDULE_MODEL_PARTITION_REQUIRED" in _codes(report)


def test_missing_invalid_and_empty_sources_are_safe_reports(tmp_path) -> None:
    empty = inspect_migration_012()
    assert empty.preflight_clear is True
    assert _codes(empty) == {"MIGRATION_NO_LEGACY_SOURCES"}

    invalid = tmp_path / "schedules.json"
    invalid.write_text("{secret invalid", encoding="utf-8")
    report = inspect_migration_012(
        learning_sqlite_path=tmp_path / "absent.db",
        schedule_json_path=invalid,
    )
    assert "MIGRATION_LEARNING_SOURCE_MISSING" in _codes(report)
    assert "MIGRATION_SCHEDULE_SOURCE_INVALID" in _codes(report)
    assert "secret invalid" not in json.dumps(report.to_dict())


def test_preflight_rejects_ambiguous_configuration() -> None:
    mapping = LegacyLearningScopeMapping(
        source_namespace="global",
        action=LegacyScopeAction.QUARANTINE,
    )
    with pytest.raises(ValueError, match="duplicate source keys"):
        inspect_migration_012(scope_mappings=(mapping, mapping))
    with pytest.raises(ValueError, match="safe SQLite identifiers"):
        inspect_migration_012(learning_table_names=("unsafe;drop",))
    with pytest.raises(ValueError, match="IANA timezone"):
        inspect_migration_012(schedule_default_timezone="not/a-zone")


def test_cli_check_has_stable_json_and_blocked_exit_code(tmp_path) -> None:
    schedule = tmp_path / "schedules.json"
    schedule.write_text(
        json.dumps(
            {
                "jobs": [{"name": "daily", "schedule": "0 8 * * *", "prompt": "report"}],
                "runs": [],
            }
        ),
        encoding="utf-8",
    )
    runner = CliRunner()

    blocked = runner.invoke(
        cli,
        ["migrate", "0.12", "check", "--schedules", str(schedule), "--json"],
    )

    assert blocked.exit_code == 3
    blocked_payload = json.loads(blocked.output)
    assert blocked_payload["preflight_clear"] is False
    assert blocked_payload["apply_allowed"] is False

    clear = runner.invoke(
        cli,
        [
            "migrate",
            "0.12",
            "check",
            "--schedules",
            str(schedule),
            "--timezone",
            "UTC",
            "--misfire-policy",
            "skip",
            "--old-writer-fence-plan",
            "stop-old-service:v1",
            "--json",
        ],
    )
    assert clear.exit_code == 0
    assert json.loads(clear.output)["preflight_clear"] is True

    invalid_map = tmp_path / "scope-map.json"
    invalid_map.write_text('{"secret":"do-not-echo"', encoding="utf-8")
    invalid = runner.invoke(
        cli,
        [
            "migrate",
            "0.12",
            "check",
            "--scope-map-file",
            str(invalid_map),
            "--json",
        ],
    )
    assert invalid.exit_code == 1
    assert "do-not-echo" not in invalid.output
    assert "Raw parser details are redacted" in invalid.output
