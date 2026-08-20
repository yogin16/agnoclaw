"""PostgreSQL/service migration control-plane contracts."""

from __future__ import annotations

import json
import stat
from dataclasses import FrozenInstanceError, replace

import pytest

from agnoclaw.migration import LegacyLearningScopeMapping, LegacyScopeAction
from agnoclaw.migration_apply import Migration012Error
from agnoclaw.migration_service import (
    PostgresMigrationBackupReceipt,
    PostgresMigrationDatabaseRef,
    PostgresMigrationTableEvidence,
    _create_postgres_migration_012_plan_unchecked,
    load_postgres_schedule_map,
    read_postgres_migration_012_plan,
    write_postgres_migration_012_plan,
)


def _schedule_map(path, *, duplicate: bool = False, **changes):
    rule = {
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
    rule.update(changes)
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.1",
                "schedules": [rule, dict(rule)] if duplicate else [rule],
            }
        ),
        encoding="utf-8",
    )
    return load_postgres_schedule_map(path)


def _plan(tmp_path):
    schedule_map = _schedule_map(tmp_path / "schedule-map.json")
    evidence = (
        PostgresMigrationTableEvidence(
            role="source",
            table_name="agno_learnings",
            row_count=2,
            schema_digest="sha256:" + "1" * 64,
            logical_digest="sha256:" + "2" * 64,
        ),
        PostgresMigrationTableEvidence(
            role="source",
            table_name="agno_schedules",
            row_count=1,
            schema_digest="sha256:" + "3" * 64,
            logical_digest="sha256:" + "4" * 64,
        ),
    )
    mapping = LegacyLearningScopeMapping(
        source_namespace="global",
        learning_type="learned_knowledge",
        action=LegacyScopeAction.QUARANTINE,
    )
    return _create_postgres_migration_012_plan_unchecked(
        source=PostgresMigrationDatabaseRef("source", "AGNO_SOURCE_DSN", "ai"),
        target_learning=PostgresMigrationDatabaseRef("target_learning", "AGNO_TARGET_DSN", "ai"),
        target_runtime=PostgresMigrationDatabaseRef("target_runtime", "AGNO_TARGET_DSN", "public"),
        target_tenant_id="tenant-a",
        target_agent_id="reviewer",
        schedule_map=schedule_map,
        backup_receipt=PostgresMigrationBackupReceipt(
            receipt_id="backup-object-v7",
            receipt_digest="sha256:" + "5" * 64,
            restore_test_id="restore-drill-42",
        ),
        writer_fence_plan="deployment-stop:v3",
        endpoint_evidence_digests=(
            ("source", "sha256:" + "6" * 64),
            ("target", "sha256:" + "7" * 64),
        ),
        table_evidence=evidence,
        scope_mappings=(mapping,),
    )


def test_database_ref_serializes_reference_and_resolves_without_leaking() -> None:
    ref = PostgresMigrationDatabaseRef("source", "AGNO_SOURCE_DSN", "ai")
    assert ref.to_dict() == {
        "role": "source",
        "credential_env": "AGNO_SOURCE_DSN",
        "schema": "ai",
    }
    assert ref.resolve({"AGNO_SOURCE_DSN": "postgresql://user:secret@db/app"}) == (
        "postgresql://user:secret@db/app"
    )
    with pytest.raises(ValueError):
        PostgresMigrationDatabaseRef("source", "postgresql://secret", "ai")
    with pytest.raises(Migration012Error) as missing:
        ref.resolve({})
    assert missing.value.code == "MIGRATION_POSTGRES_CREDENTIAL_UNAVAILABLE"
    assert "secret" not in str(missing.value)


def test_schedule_map_is_digest_bound_and_duplicate_safe(tmp_path) -> None:
    schedule_map = _schedule_map(tmp_path / "schedule-map.json")
    summary = schedule_map.summary_dict()
    assert summary["count"] == 1
    assert summary["map_digest"].startswith("sha256:")
    assert "private incident prompt" not in json.dumps(summary)

    with pytest.raises(Migration012Error) as duplicate:
        _schedule_map(tmp_path / "duplicate.json", duplicate=True)
    assert duplicate.value.code == "MIGRATION_SCHEDULE_MAP_DUPLICATE"
    with pytest.raises(ValueError, match="map digest"):
        replace(schedule_map, map_digest="sha256:" + "0" * 64)
    with pytest.raises(ValueError, match="map digest"):
        replace(
            schedule_map,
            rules=(replace(schedule_map.rules[0], schedule="1h"),),
        )
    with pytest.raises(ValueError, match="overlap_policy"):
        replace(schedule_map.rules[0], overlap_policy="allow")

    legacy_path = tmp_path / "legacy-schedule-map.json"
    legacy = json.loads((tmp_path / "schedule-map.json").read_text(encoding="utf-8"))
    legacy["schema_version"] = "1.0"
    legacy_path.write_text(json.dumps(legacy), encoding="utf-8")
    with pytest.raises(Migration012Error) as unsupported:
        load_postgres_schedule_map(legacy_path)
    assert unsupported.value.code == "MIGRATION_SCHEDULE_MAP_SCHEMA_UNSUPPORTED"


def test_backup_and_fence_references_reject_secret_shaped_values(tmp_path) -> None:
    with pytest.raises(ValueError, match="opaque non-secret"):
        PostgresMigrationBackupReceipt(
            receipt_id="postgresql://user:secret@db/app",
            receipt_digest="sha256:" + "5" * 64,
            restore_test_id="restore-drill-42",
        )
    plan = _plan(tmp_path)
    with pytest.raises(ValueError, match="opaque non-secret"):
        replace(plan, writer_fence_plan="deployment stop with secret")


def test_service_plan_rejects_schedule_authority_outside_plan(tmp_path) -> None:
    plan = _plan(tmp_path)
    schedule_map = _schedule_map(tmp_path / "other-tenant.json", tenant_id="tenant-b")

    with pytest.raises(ValueError, match="trusted target tenant and agent"):
        _create_postgres_migration_012_plan_unchecked(
            source=plan.source,
            target_learning=plan.target_learning,
            target_runtime=plan.target_runtime,
            target_tenant_id=plan.target_tenant_id,
            target_agent_id=plan.target_agent_id,
            schedule_map=schedule_map,
            backup_receipt=plan.backup_receipt,
            writer_fence_plan=plan.writer_fence_plan,
            endpoint_evidence_digests=plan.endpoint_evidence_digests,
            table_evidence=plan.table_evidence,
            target_org_id=plan.target_org_id,
            scope_mappings=plan.scope_mappings,
        )


def test_service_plan_is_content_free_mode_0600_and_tamper_evident(tmp_path) -> None:
    plan = _plan(tmp_path)
    with pytest.raises(FrozenInstanceError):
        plan.schedule_map_summary.count = 2  # type: ignore[misc]
    path = write_postgres_migration_012_plan(tmp_path / "plan.json", plan)
    payload = path.read_text(encoding="utf-8")

    assert "private incident prompt" not in payload
    assert "postgresql://" not in payload
    assert "secret" not in payload
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert read_postgres_migration_012_plan(path).plan_digest == plan.plan_digest

    changed = json.loads(payload)
    changed["target_authority"]["tenant_id"] = "tenant-b"
    path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(Migration012Error) as tampered:
        read_postgres_migration_012_plan(path)
    assert tampered.value.code == "MIGRATION_PLAN_DIGEST_MISMATCH"


def test_plan_rejects_non_summary_schedule_content_even_with_recomputed_digest(
    tmp_path,
) -> None:
    plan = _plan(tmp_path)
    payload = plan.to_dict()
    payload["schedule_map"]["prompt"] = "must-not-enter-plan"

    with pytest.raises(ValueError, match="schedule_map_summary"):
        type(plan).from_dict(payload)


def test_service_plan_writer_refuses_symbolic_link_destination(tmp_path) -> None:
    plan = _plan(tmp_path)
    protected = tmp_path / "protected.json"
    protected.write_text("keep-me", encoding="utf-8")
    destination = tmp_path / "plan-link.json"
    destination.symlink_to(protected)

    with pytest.raises(Migration012Error) as unsafe:
        write_postgres_migration_012_plan(destination, plan)

    assert unsafe.value.code == "MIGRATION_CONTROL_PATH_UNSAFE"
    assert protected.read_text(encoding="utf-8") == "keep-me"
