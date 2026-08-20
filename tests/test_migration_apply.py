"""Certified local 0.12 migration contracts."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from agnoclaw.cli.main import cli
from agnoclaw.migration import LegacyLearningScopeMapping, LegacyScopeAction
from agnoclaw.migration_apply import (
    Migration012Error,
    apply_migration_012,
    create_migration_012_plan,
    cutover_migration_012,
    read_migration_012_plan,
    rollback_migration_012,
    verify_migration_012,
    write_migration_012_plan,
)
from agnoclaw.runtime import JsonSchedulerBackend, SchedulerJob


def _plan(root):
    learning = root / "legacy.db"
    connection = sqlite3.connect(learning)
    connection.execute(
        "CREATE TABLE agno_learnings (learning_id TEXT PRIMARY KEY, "
        "learning_type TEXT NOT NULL, namespace TEXT, user_id TEXT, "
        "agent_id TEXT, team_id TEXT, workflow_id TEXT, session_id TEXT, "
        "entity_id TEXT, entity_type TEXT, content TEXT, metadata TEXT, "
        "created_at INTEGER, updated_at INTEGER)"
    )
    connection.execute(
        "INSERT INTO agno_learnings(learning_id,learning_type,namespace,"
        "user_id,content) VALUES(?,?,?,?,?)",
        ("p1", "user_profile", "user", "legacy-user", '{"name":"Ada"}'),
    )
    connection.execute(
        "INSERT INTO agno_learnings(learning_id,learning_type,namespace,content) VALUES(?,?,?,?)",
        ("k1", "learned_knowledge", "global", '{"fact":"private"}'),
    )
    connection.commit()
    connection.close()
    schedules = root / "schedules.json"
    schedules.write_text(
        json.dumps(
            {
                "jobs": [{"name": "daily", "schedule": "1h", "prompt": "report"}],
                "runs": [],
            }
        ),
        encoding="utf-8",
    )
    mapping = LegacyLearningScopeMapping(
        source_namespace="global",
        learning_type="learned_knowledge",
        action=LegacyScopeAction.QUARANTINE,
    )
    return create_migration_012_plan(
        learning_sqlite_path=learning,
        schedule_json_path=schedules,
        target_learning_db=root / "target.db",
        target_runtime_db=root / "runtime.db",
        target_tenant_id="tenant-a",
        target_agent_id="agent-a",
        scope_mappings=(mapping,),
        schedule_default_timezone="UTC",
        schedule_default_misfire_policy="skip",
        old_writer_fence_plan="stop-service:v1",
    )


def test_plan_is_content_free_and_digest_bound(tmp_path):
    plan = _plan(tmp_path)
    path = write_migration_012_plan(tmp_path / "plan.json", plan)
    payload = path.read_text()
    assert '"fact":"private"' not in payload
    assert read_migration_012_plan(path).plan_digest == plan.plan_digest
    changed = json.loads(payload)
    changed["target_authority"]["tenant_id"] = "changed"
    path.write_text(json.dumps(changed))
    with pytest.raises(Migration012Error) as raised:
        read_migration_012_plan(path)
    assert raised.value.code == "MIGRATION_PLAN_DIGEST_MISMATCH"


def test_apply_verify_cutover_and_rollback(tmp_path):
    plan = _plan(tmp_path)
    state = tmp_path / "state"
    applied = apply_migration_012(
        plan,
        state_dir=state,
        confirm_plan_digest=plan.plan_digest,
        writers_stopped=True,
    )
    assert applied["phase"] == "applied"
    assert applied["imports"]["learning"]["personal_rows"] == 1
    assert applied["imports"]["learning"]["quarantined_rows"] == 1
    assert applied["imports"]["schedule"]["jobs"] == 1
    with pytest.raises(Exception) as fenced:
        JsonSchedulerBackend(plan.schedule_source).upsert_job(
            SchedulerJob(name="blocked", schedule="1h", prompt="blocked")
        )
    assert getattr(fenced.value, "code", None) == "SCHEDULER_STORE_FENCED"
    assert verify_migration_012(state_dir=state)["phase"] == "verified"
    cutover = cutover_migration_012(state_dir=state, confirm_migration_id=plan.migration_id)
    assert cutover["phase"] == "cutover"
    assert verify_migration_012(state_dir=state)["phase"] == "cutover"
    rollback = rollback_migration_012(
        state_dir=state,
        confirm_migration_id=plan.migration_id,
        writers_stopped=True,
    )
    assert rollback["phase"] == "rolled_back"
    assert not (tmp_path / "target.db").exists()
    assert not (tmp_path / "runtime.db").exists()


def test_apply_blocks_source_drift_and_missing_confirmation(tmp_path):
    plan = _plan(tmp_path)
    with pytest.raises(Migration012Error) as unconfirmed:
        apply_migration_012(
            plan,
            state_dir=tmp_path / "state",
            confirm_plan_digest=plan.plan_digest,
            writers_stopped=False,
        )
    assert unconfirmed.value.code == "MIGRATION_WRITERS_NOT_CONFIRMED_STOPPED"
    with open(plan.schedule_source, "a") as output:
        output.write(" ")
    with pytest.raises(Migration012Error) as drift:
        apply_migration_012(
            plan,
            state_dir=tmp_path / "state",
            confirm_plan_digest=plan.plan_digest,
            writers_stopped=True,
        )
    assert drift.value.code == "MIGRATION_SOURCE_DRIFT"


def test_rollback_refuses_target_drift(tmp_path):
    plan = _plan(tmp_path)
    state = tmp_path / "state"
    apply_migration_012(
        plan,
        state_dir=state,
        confirm_plan_digest=plan.plan_digest,
        writers_stopped=True,
    )
    connection = sqlite3.connect(plan.target_learning_db)
    connection.execute("UPDATE agno_learnings SET updated_at = 99")
    connection.commit()
    connection.close()
    with pytest.raises(Migration012Error) as drift:
        rollback_migration_012(
            state_dir=state,
            confirm_migration_id=plan.migration_id,
            writers_stopped=True,
        )
    assert drift.value.code == "MIGRATION_TARGET_DRIFT"


def test_cli_exposes_stable_noninteractive_migration_workflow(tmp_path):
    plan = _plan(tmp_path)
    plan_path = tmp_path / "plan.json"
    write_migration_012_plan(plan_path, plan)
    runner = CliRunner()
    state = tmp_path / "state"
    missing_confirmation = runner.invoke(
        cli,
        [
            "migrate",
            "0.12",
            "apply",
            "--plan",
            str(plan_path),
            "--state-dir",
            str(state),
            "--confirm-plan",
            plan.plan_digest,
            "--json",
        ],
    )
    assert missing_confirmation.exit_code == 3
    error = json.loads(missing_confirmation.stderr)
    assert error["ok"] is False
    assert error["error"]["code"] == "MIGRATION_WRITERS_NOT_CONFIRMED_STOPPED"

    applied = runner.invoke(
        cli,
        [
            "migrate",
            "0.12",
            "apply",
            "--plan",
            str(plan_path),
            "--state-dir",
            str(state),
            "--confirm-plan",
            plan.plan_digest,
            "--writers-stopped",
            "--json",
        ],
    )
    assert applied.exit_code == 0
    assert json.loads(applied.stdout)["result"]["phase"] == "applied"

    verified = runner.invoke(
        cli,
        ["migrate", "0.12", "verify", "--state-dir", str(state), "--json"],
    )
    assert verified.exit_code == 0
    assert json.loads(verified.stdout)["result"]["phase"] == "verified"

    unconfirmed_rollback = runner.invoke(
        cli,
        [
            "migrate",
            "0.12",
            "rollback",
            "--state-dir",
            str(state),
            "--confirm-migration",
            plan.migration_id,
            "--json",
        ],
    )
    assert unconfirmed_rollback.exit_code == 3
    assert (
        json.loads(unconfirmed_rollback.stderr)["error"]["code"]
        == "MIGRATION_WRITERS_NOT_CONFIRMED_STOPPED"
    )


def test_binary_legacy_values_are_imported_deterministically(tmp_path):
    plan = _plan(tmp_path)
    connection = sqlite3.connect(plan.learning_source)
    connection.execute(
        "UPDATE agno_learnings SET content = ? WHERE learning_id = 'k1'",
        (sqlite3.Binary(b"\x00private\xff"),),
    )
    connection.commit()
    connection.close()
    # Re-plan so the source checksum binds the binary value.
    plan = create_migration_012_plan(
        learning_sqlite_path=plan.learning_source,
        target_learning_db=plan.target_learning_db,
        target_tenant_id="tenant-a",
        target_agent_id="agent-a",
        scope_mappings=plan.scope_mappings,
    )

    applied = apply_migration_012(
        plan,
        state_dir=tmp_path / "binary-state",
        confirm_plan_digest=plan.plan_digest,
        writers_stopped=True,
    )

    assert applied["imports"]["learning"]["quarantined_rows"] == 1
    assert verify_migration_012(state_dir=tmp_path / "binary-state")["phase"] == "verified"


def test_run_once_preserves_one_overdue_legacy_occurrence(tmp_path):
    schedule = tmp_path / "schedules.json"
    due_at = "2020-01-01T00:00:00+00:00"
    schedule.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "name": "overdue",
                        "schedule": "1h",
                        "prompt": "run once",
                        "next_run_at": due_at,
                    }
                ],
                "runs": [],
            }
        ),
        encoding="utf-8",
    )
    plan = create_migration_012_plan(
        schedule_json_path=schedule,
        target_runtime_db=tmp_path / "runtime.db",
        schedule_default_timezone="UTC",
        schedule_default_misfire_policy="run_once",
        old_writer_fence_plan="stop-service:v1",
    )

    apply_migration_012(
        plan,
        state_dir=tmp_path / "state",
        confirm_plan_digest=plan.plan_digest,
        writers_stopped=True,
    )
    store = JsonSchedulerBackend(schedule)
    assert store.get_job("overdue") is not None
    store = None
    from agnoclaw.runtime import RuntimeSchedulerBackend, SQLiteRuntimeStore

    runtime = SQLiteRuntimeStore(str(tmp_path / "runtime.db"))
    try:
        migrated = RuntimeSchedulerBackend(runtime).get_job("overdue")
        assert migrated is not None
        assert migrated.next_run_at == due_at
        assert migrated.misfire_policy == "fire_once"
    finally:
        runtime.close()


def test_apply_resumes_after_learning_import_interruption(tmp_path, monkeypatch):
    plan = _plan(tmp_path)
    state = tmp_path / "state"
    from agnoclaw import migration_apply

    original_apply_schedule = migration_apply._apply_schedule

    def interrupt_schedule(_plan):
        raise OSError("injected apply interruption")

    monkeypatch.setattr(migration_apply, "_apply_schedule", interrupt_schedule)
    with pytest.raises(OSError, match="injected apply interruption"):
        apply_migration_012(
            plan,
            state_dir=state,
            confirm_plan_digest=plan.plan_digest,
            writers_stopped=True,
        )
    assert json.loads((state / "manifest.json").read_text())["phase"] == "backed_up"

    monkeypatch.setattr(migration_apply, "_apply_schedule", original_apply_schedule)
    resumed = apply_migration_012(
        plan,
        state_dir=state,
        confirm_plan_digest=plan.plan_digest,
        writers_stopped=True,
    )
    assert resumed["phase"] == "applied"
    assert verify_migration_012(state_dir=state)["phase"] == "verified"


def test_plan_blocks_post_rekey_personal_identity_collisions(tmp_path):
    plan = _plan(tmp_path)
    connection = sqlite3.connect(plan.learning_source)
    connection.execute(
        "INSERT INTO agno_learnings(learning_id,learning_type,namespace,"
        "user_id,agent_id,content) VALUES(?,?,?,?,?,?)",
        ("p2", "user_profile", "user", "legacy-user", "other-agent", '{"name":"Grace"}'),
    )
    connection.commit()
    connection.close()

    with pytest.raises(Migration012Error) as collision:
        create_migration_012_plan(
            learning_sqlite_path=plan.learning_source,
            target_learning_db=plan.target_learning_db,
            target_tenant_id="tenant-a",
            target_agent_id="agent-a",
            scope_mappings=plan.scope_mappings,
        )
    assert collision.value.code == "MIGRATION_TARGET_IDENTITY_COLLISION"


def test_rollback_resumes_after_partial_multi_target_restore(tmp_path, monkeypatch):
    plan = _plan(tmp_path)
    for target_text in (plan.target_learning_db, plan.target_runtime_db):
        connection = sqlite3.connect(target_text)
        connection.execute("CREATE TABLE preexisting (value TEXT)")
        connection.execute("INSERT INTO preexisting VALUES ('before')")
        connection.commit()
        connection.close()
    state = tmp_path / "state"
    apply_migration_012(
        plan,
        state_dir=state,
        confirm_plan_digest=plan.plan_digest,
        writers_stopped=True,
    )

    from agnoclaw import migration_apply

    original_copy = migration_apply.shutil.copy2
    restored = 0

    def interrupt_second_restore(source, target, *args, **kwargs):
        nonlocal restored
        if ".rollback-" in Path(target).name:
            restored += 1
            if restored == 2:
                raise OSError("injected rollback interruption")
        return original_copy(source, target, *args, **kwargs)

    monkeypatch.setattr(migration_apply.shutil, "copy2", interrupt_second_restore)
    with pytest.raises(OSError, match="injected rollback interruption"):
        rollback_migration_012(
            state_dir=state,
            confirm_migration_id=plan.migration_id,
            writers_stopped=True,
        )
    assert json.loads((state / "manifest.json").read_text())["phase"] == "rolling_back"

    monkeypatch.setattr(migration_apply.shutil, "copy2", original_copy)
    resumed = rollback_migration_012(
        state_dir=state,
        confirm_migration_id=plan.migration_id,
        writers_stopped=True,
    )
    assert resumed["phase"] == "rolled_back"
    for target_text in (plan.target_learning_db, plan.target_runtime_db):
        connection = sqlite3.connect(target_text)
        try:
            assert connection.execute("SELECT value FROM preexisting").fetchone() == ("before",)
        finally:
            connection.close()
