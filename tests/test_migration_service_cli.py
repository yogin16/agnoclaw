"""Automation-safe CLI contracts for PostgreSQL/service migration planning."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import replace
from uuid import uuid4

import pytest
from click.testing import CliRunner

import agnoclaw
from agnoclaw.cli.main import cli
from agnoclaw.migration import MigrationFinding, MigrationSeverity
from agnoclaw.migration_apply import Migration012Error
from agnoclaw.migration_service import (
    PostgresMigrationDatabaseRef,
    PostgresMigrationTableEvidence,
    load_postgres_schedule_map,
    read_postgres_migration_012_plan,
)
from agnoclaw.migration_service_apply import PostgresMigration012LifecycleReceipt
from agnoclaw.migration_service_scan import PostgresMigration012ScanReport
from agnoclaw.migration_service_transform import PostgresMigration012TransformReport


def _digest(value) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _schedule_map(tmp_path):
    path = tmp_path / "private-schedule-map.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.1",
                "schedules": [
                    {
                        "source_schedule_id": "daily-review",
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
    return path


def _references():
    return (
        PostgresMigrationDatabaseRef("source", "AGNO_SOURCE_DSN", "agno"),
        PostgresMigrationDatabaseRef("target_learning", "AGNO_TARGET_DSN", "agno"),
        PostgresMigrationDatabaseRef("target_runtime", "AGNO_TARGET_DSN", "agnoclaw_runtime"),
    )


def _report(schedule_map_path, *, findings=()):
    references = _references()
    schedule_map = load_postgres_schedule_map(schedule_map_path)
    return PostgresMigration012ScanReport(
        scanned_at="2026-08-14T00:00:00+00:00",
        database_reference_digests=tuple(
            sorted((item.role, _digest(item.to_dict())) for item in references)
        ),
        endpoint_evidence_digests=(
            ("source", "sha256:" + "1" * 64),
            ("target_learning", "sha256:" + "2" * 64),
            ("target_runtime", "sha256:" + "3" * 64),
        ),
        table_evidence=(
            PostgresMigrationTableEvidence(
                role="source",
                table_name="agno_learnings",
                row_count=0,
                schema_digest="sha256:" + "4" * 64,
                logical_digest="sha256:" + "5" * 64,
            ),
        ),
        findings=tuple(findings),
        source_schedule_id_set_digest=schedule_map.source_id_set_digest,
        scope_mapping_digest=_digest([]),
    )


def _common_args(schedule_map_path):
    return [
        "--source-credential-env",
        "AGNO_SOURCE_DSN",
        "--target-learning-credential-env",
        "AGNO_TARGET_DSN",
        "--target-runtime-credential-env",
        "AGNO_TARGET_DSN",
        "--schedule-map-file",
        str(schedule_map_path),
    ]


def _plan_args(schedule_map_path, output):
    return [
        "migrate",
        "0.12",
        "service",
        "plan",
        "--target-tenant-id",
        "tenant-a",
        "--target-agent-id",
        "reviewer",
        "--backup-receipt-id",
        "backup-object-v7",
        "--backup-receipt-digest",
        "sha256:" + "6" * 64,
        "--restore-test-id",
        "restore-drill-42",
        "--writer-fence-plan",
        "deployment-stop:v3",
        "--output",
        str(output),
        *_common_args(schedule_map_path),
        "--json",
    ]


def _transform_report(plan) -> PostgresMigration012TransformReport:
    counts = tuple(
        sorted(
            (
                ("learning_personal", 0),
                ("learning_quarantine", 0),
                ("schedule_job", 1),
                ("schedule_history", 0),
            )
        )
    )
    report = PostgresMigration012TransformReport(
        migration_id=plan.migration_id,
        plan_digest=plan.plan_digest,
        generated_at="2026-08-14T00:00:00+00:00",
        counts=counts,
        category_digests=tuple((name, "sha256:" + "7" * 64) for name, _ in counts),
        source_table_digests=tuple(
            (table, "sha256:" + "8" * 64)
            for table in ("agno_learnings", "agno_schedules", "agno_schedule_runs")
        ),
        target_identity_digest="sha256:" + "9" * 64,
    )
    return replace(report, transform_digest=report.computed_transform_digest)


def _lifecycle_receipt(plan, *, operation: str, phase: str):
    receipt = PostgresMigration012LifecycleReceipt(
        operation=operation,
        migration_id=plan.migration_id,
        plan_digest=plan.plan_digest,
        transform_digest=_transform_report(plan).transform_digest,
        generated_at="2026-08-14T00:00:00+00:00",
        role_phases=(("target_learning", phase), ("target_runtime", phase)),
        counts=(("target_runtime", "schedule_job", "inserted", 1),),
        rollback_available=phase != "rolled_back",
    )
    return replace(receipt, receipt_digest=receipt.computed_receipt_digest)


def _lifecycle_args(plan_path, schedule_map_path, plan, transform_digest):
    return [
        "--plan",
        str(plan_path),
        "--schedule-map-file",
        str(schedule_map_path),
        "--confirm-plan-digest",
        plan.plan_digest,
        "--confirm-transform-digest",
        transform_digest,
        "--confirm-writer-fence-plan",
        plan.writer_fence_plan,
        "--writers-stopped",
        "--json",
    ]


def test_service_check_json_is_stdout_only_content_free_and_actionable(
    tmp_path, monkeypatch
) -> None:
    schedule_map_path = _schedule_map(tmp_path)
    report = _report(schedule_map_path)
    captured = {}

    def fake_scan(**kwargs):
        captured.update(kwargs)
        return report

    monkeypatch.setattr(agnoclaw, "scan_postgres_migration_012", fake_scan)
    result = CliRunner().invoke(
        cli,
        [
            "migrate",
            "0.12",
            "service",
            "check",
            *_common_args(schedule_map_path),
            "--batch-size",
            "17",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["command"] == "service.check"
    assert payload["status"] == "ok"
    assert payload["ok"] is True
    assert payload["result"]["ready"] is True
    assert payload["next_command"] == "agnoclaw migrate 0.12 service plan --help"
    assert captured["batch_size"] == 17
    assert captured["source"].credential_env == "AGNO_SOURCE_DSN"
    assert "private incident prompt" not in result.stdout
    assert "postgresql://" not in result.stdout


def test_service_check_blockers_use_stdout_and_exit_three(tmp_path, monkeypatch) -> None:
    schedule_map_path = _schedule_map(tmp_path)
    blocker = MigrationFinding(
        code="MIGRATION_POSTGRES_SCHEDULE_IN_FLIGHT",
        severity=MigrationSeverity.BLOCKER,
        source="source:agno_schedules",
        safe_message="Source schedules are locked or in flight.",
        resolution="Stop scheduler writers and wait for completion.",
        count=2,
    )
    report = _report(schedule_map_path, findings=(blocker,))
    monkeypatch.setattr(agnoclaw, "scan_postgres_migration_012", lambda **_: report)

    result = CliRunner().invoke(
        cli,
        [
            "migrate",
            "0.12",
            "service",
            "check",
            *_common_args(schedule_map_path),
            "--json",
        ],
    )

    assert result.exit_code == 3
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["status"] == "blocked"
    assert payload["ok"] is False
    assert payload["result"]["findings"][0]["count"] == 2


def test_service_check_errors_are_structured_retryable_and_secret_free(
    tmp_path, monkeypatch
) -> None:
    schedule_map_path = _schedule_map(tmp_path)

    def fail_scan(**_):
        raise Migration012Error(
            "MIGRATION_POSTGRES_SCAN_FAILED",
            "A PostgreSQL endpoint could not be scanned safely.",
            endpoint_roles=["source"],
        )

    monkeypatch.setattr(agnoclaw, "scan_postgres_migration_012", fail_scan)
    result = CliRunner().invoke(
        cli,
        [
            "migrate",
            "0.12",
            "service",
            "check",
            *_common_args(schedule_map_path),
            "--json",
        ],
        env={"AGNO_SOURCE_DSN": "postgresql://user:supersecret@db/private"},
    )

    assert result.exit_code == 75
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["status"] == "error"
    assert payload["exit_code"] == 75
    assert payload["error"]["transient"] is True
    assert "supersecret" not in result.stderr


def test_service_check_configuration_errors_use_exit_78(tmp_path, monkeypatch) -> None:
    schedule_map_path = _schedule_map(tmp_path)

    def fail_scan(**_):
        raise Migration012Error(
            "MIGRATION_POSTGRES_CREDENTIAL_UNAVAILABLE",
            "A required PostgreSQL credential reference is unavailable.",
            credential_env="AGNO_SOURCE_DSN",
        )

    monkeypatch.setattr(agnoclaw, "scan_postgres_migration_012", fail_scan)
    result = CliRunner().invoke(
        cli,
        [
            "migrate",
            "0.12",
            "service",
            "check",
            *_common_args(schedule_map_path),
            "--json",
        ],
    )

    assert result.exit_code == 78
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["exit_code"] == 78
    assert payload["error"]["code"] == "MIGRATION_POSTGRES_CREDENTIAL_UNAVAILABLE"
    assert payload["error"]["transient"] is False
    assert payload["error"]["fix"]


def test_service_help_discovers_full_lifecycle_without_dsn_valued_flags() -> None:
    runner = CliRunner()
    service_help = runner.invoke(cli, ["migrate", "0.12", "service", "--help"])
    check_help = runner.invoke(cli, ["migrate", "0.12", "service", "check", "--help"])
    apply_help = runner.invoke(cli, ["migrate", "0.12", "service", "apply", "--help"])

    assert service_help.exit_code == 0
    for command in ("check", "plan", "preview", "apply", "verify", "cutover", "rollback"):
        assert command in service_help.stdout
    assert check_help.exit_code == 0
    assert "Examples:" in check_help.stdout
    assert "--source-credential-env ENV_NAME" in check_help.stdout
    assert "--source-dsn" not in check_help.stdout
    assert apply_help.exit_code == 0
    assert "Examples:" in apply_help.stdout
    assert "--dry-run" in apply_help.stdout
    assert "--writers-stopped" in apply_help.stdout
    assert "--confirm-transform-digest SHA256" in apply_help.stdout
    assert "--source-dsn" not in apply_help.stdout


def test_service_plan_is_scan_bound_noninteractive_and_refuses_overwrite(
    tmp_path, monkeypatch
) -> None:
    schedule_map_path = _schedule_map(tmp_path)
    report = _report(schedule_map_path)
    monkeypatch.setattr(agnoclaw, "scan_postgres_migration_012", lambda **_: report)
    output = tmp_path / "service-plan.json"
    args = _plan_args(schedule_map_path, output)

    created = CliRunner().invoke(cli, args)
    assert created.exit_code == 0, created.output
    assert created.stderr == ""
    payload = json.loads(created.stdout)
    assert payload["command"] == "service.plan"
    assert payload["result"]["apply_available"] is False
    assert "exact service preview" in payload["next_action"]
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    plan = read_postgres_migration_012_plan(output)
    assert payload["result"]["plan"]["plan_digest"] == plan.plan_digest
    assert "private incident prompt" not in output.read_text(encoding="utf-8")

    refused = CliRunner().invoke(cli, args)
    assert refused.exit_code == 3
    assert refused.stdout == ""
    error = json.loads(refused.stderr)
    assert error["error"]["code"] == "MIGRATION_PLAN_OUTPUT_EXISTS"
    assert error["error"]["transient"] is False


def test_service_plan_blockers_never_create_output(tmp_path, monkeypatch) -> None:
    schedule_map_path = _schedule_map(tmp_path)
    blocker = MigrationFinding(
        code="MIGRATION_POSTGRES_LEARNING_OWNER_MISSING",
        severity=MigrationSeverity.BLOCKER,
        source="source:agno_learnings",
        safe_message="Personal learning rows lack owners.",
        resolution="Repair or quarantine those records.",
        count=1,
    )
    report = replace(_report(schedule_map_path), findings=(blocker,))
    monkeypatch.setattr(agnoclaw, "scan_postgres_migration_012", lambda **_: report)
    output = tmp_path / "blocked-plan.json"

    result = CliRunner().invoke(cli, _plan_args(schedule_map_path, output))

    assert result.exit_code == 3
    assert result.stderr == ""
    assert json.loads(result.stdout)["status"] == "blocked"
    assert not output.exists()


def test_service_preview_is_content_free_actionable_and_non_mutating(tmp_path, monkeypatch) -> None:
    schedule_map_path = _schedule_map(tmp_path)
    scan = _report(schedule_map_path)
    monkeypatch.setattr(agnoclaw, "scan_postgres_migration_012", lambda **_: scan)
    output = tmp_path / "service-plan.json"
    planned = CliRunner().invoke(cli, _plan_args(schedule_map_path, output))
    assert planned.exit_code == 0, planned.output
    plan = read_postgres_migration_012_plan(output)
    captured = {}

    def fake_preview(**kwargs):
        captured.update(kwargs)
        return _transform_report(plan)

    monkeypatch.setattr(agnoclaw, "preview_postgres_migration_012_transforms", fake_preview)
    result = CliRunner().invoke(
        cli,
        [
            "migrate",
            "0.12",
            "service",
            "preview",
            "--plan",
            str(output),
            "--schedule-map-file",
            str(schedule_map_path),
            "--batch-size",
            "17",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["command"] == "service.preview"
    assert payload["result"]["apply_available"] is True
    assert payload["result"]["transform"]["counts"]["schedule_job"] == 1
    assert payload["result"]["transform"]["transform_digest"].startswith("sha256:")
    assert "Review the exact plan" in payload["next_action"]
    assert "service apply" in payload["next_command"]
    assert captured["batch_size"] == 17
    assert captured["plan"].plan_digest == plan.plan_digest
    assert "private incident prompt" not in result.stdout
    assert "postgresql://" not in result.stdout


def test_service_preview_drift_is_structured_integrity_failure(tmp_path, monkeypatch) -> None:
    schedule_map_path = _schedule_map(tmp_path)
    scan = _report(schedule_map_path)
    monkeypatch.setattr(agnoclaw, "scan_postgres_migration_012", lambda **_: scan)
    output = tmp_path / "service-plan.json"
    planned = CliRunner().invoke(cli, _plan_args(schedule_map_path, output))
    assert planned.exit_code == 0, planned.output

    def fail_preview(**_):
        raise Migration012Error(
            "MIGRATION_POSTGRES_PLAN_EVIDENCE_DRIFT",
            "PostgreSQL source or target evidence changed after plan review.",
        )

    monkeypatch.setattr(agnoclaw, "preview_postgres_migration_012_transforms", fail_preview)
    result = CliRunner().invoke(
        cli,
        [
            "migrate",
            "0.12",
            "service",
            "preview",
            "--plan",
            str(output),
            "--schedule-map-file",
            str(schedule_map_path),
            "--json",
        ],
    )

    assert result.exit_code == 4
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "MIGRATION_POSTGRES_PLAN_EVIDENCE_DRIFT"
    assert payload["error"]["transient"] is False


def test_service_lifecycle_cli_is_explicit_idempotent_and_content_free(
    tmp_path, monkeypatch
) -> None:
    schedule_map_path = _schedule_map(tmp_path)
    scan = _report(schedule_map_path)
    monkeypatch.setattr(agnoclaw, "scan_postgres_migration_012", lambda **_: scan)
    output = tmp_path / "service-plan.json"
    planned = CliRunner().invoke(cli, _plan_args(schedule_map_path, output))
    assert planned.exit_code == 0, planned.output
    plan = read_postgres_migration_012_plan(output)
    transform = _transform_report(plan)
    monkeypatch.setattr(
        agnoclaw,
        "preview_postgres_migration_012_transforms",
        lambda **_: transform,
    )
    captured = {}

    def fake_apply(**kwargs):
        captured["apply"] = kwargs
        return _lifecycle_receipt(plan, operation="apply", phase="applied")

    monkeypatch.setattr(agnoclaw, "apply_postgres_migration_012", fake_apply)
    common = _lifecycle_args(output, schedule_map_path, plan, transform.transform_digest)
    dry_run = CliRunner().invoke(
        cli,
        [
            "migrate",
            "0.12",
            "service",
            "apply",
            "--confirm-backup-receipt-digest",
            plan.backup_receipt.receipt_digest,
            "--dry-run",
            *common,
        ],
    )
    assert dry_run.exit_code == 0, dry_run.output
    assert json.loads(dry_run.stdout)["result"]["mutated"] is False
    assert "apply" not in captured

    applied = CliRunner().invoke(
        cli,
        [
            "migrate",
            "0.12",
            "service",
            "apply",
            "--confirm-backup-receipt-digest",
            plan.backup_receipt.receipt_digest,
            "--write-batch-size",
            "17",
            *common,
        ],
    )
    assert applied.exit_code == 0, applied.output
    applied_payload = json.loads(applied.stdout)
    assert applied_payload["result"]["receipt"]["role_phases"]["target_runtime"] == ("applied")
    assert captured["apply"]["write_batch_size"] == 17
    assert "private incident prompt" not in applied.stdout
    assert "postgresql://" not in applied.stdout

    def fake_verify(**kwargs):
        captured["verify"] = kwargs
        return _lifecycle_receipt(plan, operation="verify", phase="verified")

    monkeypatch.setattr(agnoclaw, "verify_postgres_migration_012", fake_verify)
    verified = CliRunner().invoke(
        cli,
        ["migrate", "0.12", "service", "verify", *common],
    )
    assert verified.exit_code == 0, verified.output
    assert json.loads(verified.stdout)["result"]["mutated"] is True

    def fake_cutover(**kwargs):
        captured["cutover"] = kwargs
        return _lifecycle_receipt(plan, operation="cutover", phase="cutover")

    monkeypatch.setattr(agnoclaw, "cutover_postgres_migration_012", fake_cutover)
    cutover = CliRunner().invoke(
        cli,
        [
            "migrate",
            "0.12",
            "service",
            "cutover",
            "--cutover-receipt-id",
            "deployment-change-42",
            "--cutover-receipt-digest",
            "sha256:" + "8" * 64,
            *common,
        ],
    )
    assert cutover.exit_code == 0, cutover.output
    assert captured["cutover"]["cutover_receipt_id"] == "deployment-change-42"

    def fake_rollback(**kwargs):
        captured["rollback"] = kwargs
        return _lifecycle_receipt(plan, operation="rollback", phase="rolled_back")

    monkeypatch.setattr(agnoclaw, "rollback_postgres_migration_012", fake_rollback)
    rolled_back = CliRunner().invoke(
        cli,
        [
            "migrate",
            "0.12",
            "service",
            "rollback",
            "--confirm-no-post-cutover-target-writes",
            *common,
        ],
    )
    assert rolled_back.exit_code == 0, rolled_back.output
    assert captured["rollback"]["confirm_no_post_cutover_target_writes"] is True
    assert json.loads(rolled_back.stdout)["result"]["receipt"]["rollback_available"] is False


@pytest.mark.skipif(
    not os.getenv("AGNOCLAW_TEST_POSTGRES_URL"),
    reason="requires the disposable PostgreSQL integration service",
)
def test_live_service_cli_read_only_boundary_and_full_lifecycle(tmp_path) -> None:
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql

    dsn = os.environ["AGNOCLAW_TEST_POSTGRES_URL"]
    prefix = "agnoclaw_cli_scan_" + uuid4().hex[:12]
    schemas = (f"{prefix}_source", f"{prefix}_learning", f"{prefix}_runtime")
    setup = psycopg.connect(dsn, autocommit=True)
    schedule_map_path = _schedule_map(tmp_path)
    output = tmp_path / "live-service-plan.json"
    try:
        with setup.cursor() as cursor:
            for schema in schemas:
                cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            cursor.execute(
                sql.SQL(
                    "CREATE TABLE {}.agno_schedules ("
                    "id text PRIMARY KEY, locked_by text, locked_at bigint)"
                ).format(sql.Identifier(schemas[0]))
            )
            cursor.execute(
                sql.SQL("INSERT INTO {}.agno_schedules VALUES (%s, NULL, NULL)").format(
                    sql.Identifier(schemas[0])
                ),
                ("daily-review",),
            )
        common = [
            "--source-credential-env",
            "AGNO_SOURCE_DSN",
            "--source-schema",
            schemas[0],
            "--target-learning-credential-env",
            "AGNO_TARGET_DSN",
            "--target-learning-schema",
            schemas[1],
            "--target-runtime-credential-env",
            "AGNO_TARGET_DSN",
            "--target-runtime-schema",
            schemas[2],
            "--schedule-map-file",
            str(schedule_map_path),
            "--batch-size",
            "1",
        ]
        environment = {"AGNO_SOURCE_DSN": dsn, "AGNO_TARGET_DSN": dsn}
        checked = CliRunner().invoke(
            cli,
            ["migrate", "0.12", "service", "check", *common, "--json"],
            env=environment,
        )
        assert checked.exit_code == 0, checked.output
        assert json.loads(checked.stdout)["result"]["ready"] is True
        planned = CliRunner().invoke(
            cli,
            [
                "migrate",
                "0.12",
                "service",
                "plan",
                "--target-tenant-id",
                "tenant-a",
                "--target-agent-id",
                "reviewer",
                "--backup-receipt-id",
                "backup-object-v7",
                "--backup-receipt-digest",
                "sha256:" + "6" * 64,
                "--restore-test-id",
                "restore-drill-42",
                "--writer-fence-plan",
                "deployment-stop:v3",
                "--output",
                str(output),
                *common,
                "--json",
            ],
            env=environment,
        )
        assert planned.exit_code == 0, planned.output
        previewed = CliRunner().invoke(
            cli,
            [
                "migrate",
                "0.12",
                "service",
                "preview",
                "--plan",
                str(output),
                "--schedule-map-file",
                str(schedule_map_path),
                "--batch-size",
                "1",
                "--json",
            ],
            env=environment,
        )
        assert previewed.exit_code == 0, previewed.output
        preview_payload = json.loads(previewed.stdout)
        assert preview_payload["result"]["transform"]["counts"]["schedule_job"] == 1
        serialized = (
            checked.stdout + planned.stdout + previewed.stdout + output.read_text(encoding="utf-8")
        )
        assert dsn not in serialized
        assert "private incident prompt" not in serialized
        with setup.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema = ANY(%s)",
                ([schemas[1], schemas[2]],),
            )
            assert cursor.fetchone()[0] == 0
            cursor.execute(
                sql.SQL("SELECT count(*) FROM {}.agno_schedules").format(sql.Identifier(schemas[0]))
            )
            assert cursor.fetchone()[0] == 1

        plan = read_postgres_migration_012_plan(output)
        transform_digest = preview_payload["result"]["transform"]["transform_digest"]
        lifecycle = _lifecycle_args(
            output,
            schedule_map_path,
            plan,
            transform_digest,
        )
        applied = CliRunner().invoke(
            cli,
            [
                "migrate",
                "0.12",
                "service",
                "apply",
                "--confirm-backup-receipt-digest",
                plan.backup_receipt.receipt_digest,
                "--read-batch-size",
                "1",
                "--write-batch-size",
                "1",
                *lifecycle,
            ],
            env=environment,
        )
        assert applied.exit_code == 0, applied.output
        assert set(json.loads(applied.stdout)["result"]["receipt"]["role_phases"].values()) == {
            "applied"
        }
        verified = CliRunner().invoke(
            cli,
            ["migrate", "0.12", "service", "verify", *lifecycle],
            env=environment,
        )
        assert verified.exit_code == 0, verified.output
        cutover = CliRunner().invoke(
            cli,
            [
                "migrate",
                "0.12",
                "service",
                "cutover",
                "--cutover-receipt-id",
                "deployment-change-42",
                "--cutover-receipt-digest",
                "sha256:" + "8" * 64,
                *lifecycle,
            ],
            env=environment,
        )
        assert cutover.exit_code == 0, cutover.output
        rolled_back = CliRunner().invoke(
            cli,
            [
                "migrate",
                "0.12",
                "service",
                "rollback",
                "--confirm-no-post-cutover-target-writes",
                "--read-batch-size",
                "1",
                "--write-batch-size",
                "1",
                *lifecycle,
            ],
            env=environment,
        )
        assert rolled_back.exit_code == 0, rolled_back.output
        lifecycle_output = applied.stdout + verified.stdout + cutover.stdout + rolled_back.stdout
        assert dsn not in lifecycle_output
        assert "private incident prompt" not in lifecycle_output
        with setup.cursor() as cursor:
            cursor.execute(
                sql.SQL("SELECT count(*) FROM {}.runtime_scheduler_jobs").format(
                    sql.Identifier(schemas[2])
                )
            )
            assert cursor.fetchone()[0] == 0
    finally:
        with setup.cursor() as cursor:
            for schema in reversed(schemas):
                cursor.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
                )
        setup.close()
