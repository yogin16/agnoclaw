"""Executable documentation quality gates."""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path
from urllib.parse import unquote

import yaml

import agnoclaw

ROOT = Path(__file__).parents[1]
DOCS = ROOT / "docs"
MARKDOWN_FILES = tuple(
    sorted(
        path
        for path in ROOT.rglob("*.md")
        if not any(part.startswith(".") for part in path.relative_to(ROOT).parts)
    )
)
LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")


def _local_link_targets(path: Path) -> set[Path]:
    targets: set[Path] = set()
    for raw_target in LINK_RE.findall(path.read_text(encoding="utf-8")):
        target = raw_target.strip().strip("<>")
        if not target or target.startswith(("#", "http://", "https://", "mailto:")):
            continue
        if target in {"URL", "url"}:  # literal authoring placeholders in skill prose
            continue
        target = unquote(target.split("#", 1)[0])
        if not target:
            continue
        targets.add((path.parent / target).resolve())
    return targets


def _fenced_code(path: Path) -> str:
    blocks: list[str] = []
    active = False
    current: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            if active:
                blocks.append("\n".join(current))
                current = []
            active = not active
        elif active:
            current.append(line)
    return "\n".join(blocks)


def test_all_local_markdown_links_resolve() -> None:
    missing: list[str] = []
    for source in MARKDOWN_FILES:
        for target in _local_link_targets(source):
            if not target.exists():
                missing.append(f"{source.relative_to(ROOT)} -> {target.relative_to(ROOT)}")
    assert not missing, "broken local documentation links:\n" + "\n".join(missing)


def test_documentation_index_covers_every_current_doc() -> None:
    indexed = _local_link_targets(DOCS / "README.md")
    omitted = [
        path.relative_to(DOCS).as_posix()
        for path in sorted(DOCS.rglob("*.md"))
        if path != DOCS / "README.md" and path.resolve() not in indexed
    ]
    assert not omitted, "docs/README.md omits:\n" + "\n".join(omitted)


def test_generated_api_reference_covers_the_exact_public_surface() -> None:
    reference = DOCS / "reference" / "api.md"
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "generate_api_reference.py"), "--check"],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout

    content = reference.read_text(encoding="utf-8")
    public_names = tuple(agnoclaw.__all__)
    assert len(public_names) == len(set(public_names))
    assert f"Public symbols: `{len(public_names)}`" in content
    assert "Public-surface digest: `sha256:" in content
    assert " at 0x" not in content
    for name in public_names:
        assert content.count(f"### `{name}`\n") == 1, name

    index = (DOCS / "README.md").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    assert "reference/api.md" in index
    assert "scripts/generate_api_reference.py --check" in workflow


def test_landing_readme_stays_focused() -> None:
    lines = (ROOT / "README.md").read_text(encoding="utf-8").splitlines()
    assert len(lines) <= 250, f"README.md has {len(lines)} lines; target is <=250"


def test_contributor_setup_installs_the_declared_dev_extra() -> None:
    for relative_path in ("README.md", "CONTRIBUTING.md"):
        content = ROOT.joinpath(relative_path).read_text(encoding="utf-8")
        assert "uv sync --extra dev" in content
        assert "uv sync --dev" not in content

    contributing = ROOT.joinpath("CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "https://github.com/yogin16/agnoclaw" in contributing
    assert "ruff check src/ tests/ scripts/" in contributing
    assert "mypy src/agnoclaw/ --ignore-missing-imports" in contributing


def test_recommended_code_does_not_reach_into_private_agent() -> None:
    offenders = [
        str(path.relative_to(ROOT)) for path in MARKDOWN_FILES if "._agent" in _fenced_code(path)
    ]
    assert not offenders, "private Agent reach-through in code blocks:\n" + "\n".join(offenders)


def test_negative_evaluation_archive_is_documented_as_safe_host_api() -> None:
    candidate_docs = (DOCS / "learning-candidates.md").read_text(encoding="utf-8")
    improvement_docs = (DOCS / "self-improvement-evaluation.md").read_text(encoding="utf-8")

    for content in (candidate_docs, improvement_docs):
        assert "query_learning_evaluation_archive(" in content
        assert "EvaluationArchiveQuery" in content
        assert "content-free" in content
        assert "model-facing" in content
        assert "artifact ID" in content
    assert "LEARNING_EVALUATION_ARCHIVE_UNSUPPORTED" in candidate_docs
    assert "Schema v5" in candidate_docs
    assert "58.87 ms p99" in candidate_docs

    evaluation_docs = (DOCS / "evaluation.md").read_text(encoding="utf-8")
    assert "scripts/benchmark_learning_evaluation_archive.py" in evaluation_docs
    assert "production_certification" in evaluation_docs


def test_learning_outcome_loop_is_documented_with_its_non_mutating_boundary() -> None:
    candidates = (DOCS / "learning-candidates.md").read_text(encoding="utf-8")
    learning = (DOCS / "learning.md").read_text(encoding="utf-8")
    migration = (DOCS / "migration-0.12.md").read_text(encoding="utf-8")
    compatibility = (DOCS / "compatibility.md").read_text(encoding="utf-8")

    for api in (
        "observe_learning_application(",
        "observe_learning_outcome(",
        "summarize_learning_effectiveness(",
    ):
        assert api in candidates
    assert "merely retrieved or actually applied" in candidates
    assert "read-only recommendation" in candidates
    assert "never changes confidence" in " ".join(candidates.split())
    assert "schema v6" in candidates
    assert "do not increment the candidate revision" in " ".join(learning.split())
    assert "does not synthesize historical attribution" in " ".join(migration.split())
    assert "Learning application/outcome attribution" in compatibility


def test_resource_ownership_warnings_are_blocking() -> None:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)
    filters = set(config["tool"]["pytest"]["ini_options"]["filterwarnings"])

    assert "error::ResourceWarning" in filters
    assert "error::pytest.PytestUnraisableExceptionWarning" in filters


def test_explicit_profile_convenience_routing_is_documented_without_overclaim() -> None:
    lifecycle = (DOCS / "runtime-lifecycle.md").read_text(encoding="utf-8")
    operations = (DOCS / "operations-and-recovery.md").read_text(encoding="utf-8")
    migration = (DOCS / "migration-0.12.md").read_text(encoding="utf-8")
    cli = (DOCS / "cli.md").read_text(encoding="utf-8")
    children = (DOCS / "child-runs.md").read_text(encoding="utf-8")
    progress = (DOCS / "releases" / "v0.12.0-progress.md").read_text(encoding="utf-8")

    for content in (lifecycle, operations):
        for profile in ("`quick`", "`durable`", "`service`"):
            assert profile in content
        assert "`start()` plus `wait()`" in content
        assert "HARNESS_EVENT_LOOP_OWNERSHIP_CONFLICT" in content
        assert "legacy" in content
    assert "AgentHarness(config=HarnessConfig.legacy(), tools=[legacy_tool])" in migration
    assert "UNDERLYING_AGENT_PROFILE_UNSUPPORTED" in migration
    assert "harness-owned sync coordinator" in cli
    assert "sync chat in an explicit profile | rejected" not in lifecycle
    for code in (
        "RAW_SUBAGENT_LIFECYCLE_UNSUPPORTED",
        "TEAM_LIFECYCLE_UNSUPPORTED",
        "SKILL_LIFECYCLE_DISPATCH_UNSUPPORTED",
    ):
        assert code in lifecycle
    assert "named `legacy` profile" in children
    assert "Explicit profiles" in children and "reject named raw subagents" in children
    assert "334\n  tests with two environment skips" in progress
    assert "false ambiguous provider effect" in progress


def test_custom_model_factory_is_documented_as_fresh_owned_and_digest_bound() -> None:
    getting_started = (DOCS / "getting-started.md").read_text(encoding="utf-8")
    configuration = (DOCS / "configuration.md").read_text(encoding="utf-8")
    operations = (DOCS / "operations-and-recovery.md").read_text(encoding="utf-8")
    compatibility = (DOCS / "compatibility.md").read_text(encoding="utf-8")
    operations_prose = " ".join(operations.split())

    for content in (getting_started, configuration, operations, compatibility):
        assert "AgnoModelFactory" in content
    assert "implementation digest" in configuration
    assert "distinct instance on every call" in configuration
    assert "MODEL_FACTORY_*" in configuration
    assert "caller-injected Agno Model remains caller-owned" in operations_prose
    assert "Factory-backed process restart is certified" in operations_prose
    assert "exact digest-drift boundary" in operations_prose
    assert "narrow Agno 2.9 native tool/approval" in operations_prose


def test_public_api_journey_docs_separate_source_from_installed_proof() -> None:
    journey = (DOCS / "public-api-journey.md").read_text(encoding="utf-8")
    evaluation = (DOCS / "evaluation.md").read_text(encoding="utf-8")
    progress = (DOCS / "releases" / "v0.12.0-progress.md").read_text(encoding="utf-8")

    for content in (journey, evaluation, progress):
        assert "scripts/public_api_journey_probe.py" in content
    assert "imports only" in journey and "top-level `agnoclaw` exports" in journey
    assert "Docker networking disabled" in journey
    assert "exact candidate wheel" in journey
    assert "agno_stack_restart_probe.py" in journey
    assert "production_certification" in journey
    assert "tests/test_public_api_journey_probe.py" in evaluation
    assert "same file unchanged against the exact wheel" in evaluation


def test_exact_wheel_public_journeys_are_release_gates() -> None:
    dockerfile = ROOT / "scripts" / "release-journey.Dockerfile"
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    publish = (ROOT / ".github" / "workflows" / "publish.yml").read_text(
        encoding="utf-8"
    )

    for workflow in (ci, publish):
        assert "scripts/release-journey.Dockerfile" in workflow
        assert "--network none" in workflow
        assert "/opt/agnoclaw/public_api_journey_probe.py" in workflow
        assert "/opt/agnoclaw/agno_stack_restart_probe.py" in workflow
    content = dockerfile.read_text(encoding="utf-8")
    assert "dist/*.whl" in content
    assert "USER 65532:65532" in content
    assert "ENV HOME=/tmp/home" in content


def test_long_run_continuity_gate_is_documented_with_honest_boundaries() -> None:
    context = (DOCS / "context-management.md").read_text(encoding="utf-8")
    evaluation = (DOCS / "evaluation.md").read_text(encoding="utf-8")
    world_class = (DOCS / "world-class-harness.md").read_text(encoding="utf-8")

    for content in (context, evaluation):
        assert "scripts/long_run_continuity_probe.py" in content
        assert "--turns 100" in content
        assert "--tool-every 10" in content
        assert "head/middle/tail" in content
        assert "content-free" in content
        assert "11/11" in content
    assert "Session not found" in context
    assert "does not prove semantic summary quality" in evaluation
    assert "live provider" in evaluation
    assert "deterministic 100-turn" in world_class
    for content in (context, evaluation):
        assert "--provider ollama" in content
        assert "--allow-live-model" in content
        assert "qwen2.5:7b" in content
        assert "qwen3:0.6b" in content
        assert "11/11" in content
        assert "14 compactions" in content
        assert "1,038/1,800" in content
    assert "cloud-provider breadth" in evaluation
    assert "process death" in evaluation
    assert "ContextContinuationRecord" in context
    assert "CONTEXT_CONTINUATION_CONFLICT" in context
    for field in (
        "goal",
        "plan",
        "progress",
        "decisions",
        "approvals",
        "open_questions",
        "tests",
        "files",
        "citations",
    ):
        assert field in context


def test_context_locking_docs_define_same_host_and_multi_host_boundaries() -> None:
    context = (DOCS / "context-management.md").read_text(encoding="utf-8")
    operations = (DOCS / "operations-and-recovery.md").read_text(encoding="utf-8")
    configuration = (DOCS / "configuration.md").read_text(encoding="utf-8")

    for content in (context, operations, configuration):
        assert "LocalFileContextLockProvider" in content
    assert "CONTEXT_CROSS_PROCESS_LOCK_UNAVAILABLE" in context
    assert "CONTEXT_CROSS_PROCESS_LOCK_LOST" in context
    assert "multi-host" in context
    assert "non-cooperating" in context
    assert "shared" in operations and "exclusive" in operations


def test_postgres_ci_runs_the_bounded_load_gate() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "ci.yml"
    workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    steps = workflow["jobs"]["postgres-runtime-store"]["steps"]
    commands = "\n".join(step.get("run", "") for step in steps)

    assert "scripts/benchmark_postgres_runtime.py" in commands
    assert "scripts/benchmark_learning_evaluation_archive.py" in commands
    assert "127.0.0.1:5432/agnoclaw_test" in commands
    assert "scripts/postgres_backup_restore_probe.py" in commands
    assert "127.0.0.1:5432/agnoclaw_restore_test" in commands
    assert "--allow-target-reset" in commands
    assert "scripts/postgres_restart_probe.py" in commands
    assert "tests/test_migration_service_production_matrix.py" in commands

    failover_steps = workflow["jobs"]["postgres-fenced-promotion"]["steps"]
    failover_commands = "\n".join(step.get("run", "") for step in failover_steps)
    assert "tests/test_postgres_failover_probe.py" in failover_commands
    assert "scripts/postgres_failover_probe.py" in failover_commands
    assert "tests/test_postgres_synchronous_failover_probe.py" in failover_commands
    assert "scripts/postgres_synchronous_failover_probe.py" in failover_commands
    assert "tests/test_postgres_role_rotation_probe.py" in failover_commands
    assert "scripts/postgres_role_rotation_probe.py" in failover_commands
    assert "tests/test_postgres_writer_authority.py" in failover_commands
    assert "tests/test_postgres_writer_authority_etcd.py" in failover_commands
    assert "tests/test_etcd_writer_authority_probe.py" in failover_commands
    assert "scripts/etcd_writer_authority_probe.py" in failover_commands
    assert "tests/test_etcd_secure_quorum_probe.py" in failover_commands
    assert "scripts/etcd_secure_quorum_probe.py" in failover_commands
    assert "tests/test_postgres_split_brain_authority_probe.py" in failover_commands
    assert "scripts/postgres_split_brain_authority_probe.py" in failover_commands
    assert "--allow-topology-create" in failover_commands
    assert "--image postgres@sha256:" in failover_commands
    assert "quay.io/coreos/etcd@sha256:" in failover_commands

    failover_probe = (ROOT / "scripts" / "postgres_failover_probe.py").read_text(
        encoding="utf-8"
    )
    postgres_docs = (DOCS / "postgresql-runtime-store.md").read_text(encoding="utf-8")
    learning_docs = (DOCS / "learning.md").read_text(encoding="utf-8")
    assert "learning_ledger_streaming_replication" in failover_probe
    assert "learning candidate/evaluation/event history" in postgres_docs
    assert "owned two-node PostgreSQL gate" in learning_docs

    package_steps = workflow["jobs"]["package"]["steps"]
    package_commands = "\n".join(step.get("run", "") for step in package_steps)
    assert ".venv-wheel/bin/python -I scripts/etcd_secure_quorum_probe.py" in package_commands
    assert '"${WHEELS[0]}[otel]"' in package_commands
    assert ".venv-wheel-otel/bin/python -I scripts/smoke_otel_install.py" in package_commands
    assert ".venv-wheel-postgres/bin/agnoclaw inspect run --help" in package_commands


def test_release_workflow_publishes_the_checked_artifacts() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "publish.yml"
    workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    check_commands = "\n".join(step.get("run", "") for step in workflow["jobs"]["test"]["steps"])
    assert ".venv-release-wheel/bin/python -I scripts/etcd_secure_quorum_probe.py" in check_commands
    test_steps = workflow["jobs"]["test"]["steps"]
    publish_steps = workflow["jobs"]["publish"]["steps"]
    test_commands = "\n".join(step.get("run", "") for step in test_steps)
    publish_commands = "\n".join(step.get("run", "") for step in publish_steps)
    test_actions = {step.get("uses", "") for step in test_steps}
    publish_actions = {step.get("uses", "") for step in publish_steps}

    assert workflow["concurrency"] == {
        "group": "agnoclaw-publish",
        "cancel-in-progress": "false",
    }
    assert workflow["jobs"]["check"]["if"] == "github.ref == 'refs/heads/main'"
    assert "--cov-fail-under=80" in test_commands
    assert "ruff check src/ tests/ scripts/" in test_commands
    assert "mypy src/agnoclaw/" in test_commands
    assert "uv build --clear" in test_commands
    assert "twine check dist/*" in test_commands
    assert "dist/*.whl" in test_commands
    assert "dist/*.tar.gz" in test_commands
    assert '"${WHEELS[0]}[postgres,cli,scheduler]"' in test_commands
    assert '"${WHEELS[0]}[otel]"' in test_commands
    assert ".venv-release-otel/bin/python -I scripts/smoke_otel_install.py" in test_commands
    assert ".venv-release-postgres/bin/agnoclaw migrate 0.12 service check --help" in test_commands
    assert (
        ".venv-release-postgres/bin/agnoclaw migrate 0.12 service preview --help" in test_commands
    )
    for command in ("apply", "verify", "cutover", "rollback"):
        assert (
            f".venv-release-postgres/bin/agnoclaw migrate 0.12 service {command} --help"
            in test_commands
        )
    assert ".venv-release-postgres/bin/agnoclaw inspect run --help" in test_commands
    assert "PostgresWriterAuthorityGrant" in test_commands
    assert "EtcdPostgresWriterAuthority" in test_commands
    assert "--format cyclonedx1.5" in test_commands
    assert ".sha256" in test_commands
    assert ".provenance" in test_commands
    assert (
        "actions/upload-artifact@330a01c490aca151604b8cf639adc76d48f6c5d4"
        in test_actions
    )
    assert (
        "actions/download-artifact@634f93cb2916e3fdff6788551b99b062d0335ce0"
        in publish_actions
    )
    assert (
        "pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33"
        in publish_actions
    )
    assert "uv build" not in publish_commands


def test_observability_contract_is_installable_and_indexed() -> None:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]

    otel = project["optional-dependencies"]["otel"]
    assert any(item.startswith("opentelemetry-api>=1.44.0,<2") for item in otel)
    assert any(item.startswith("opentelemetry-sdk>=1.44.0,<2") for item in otel)
    assert any(
        item.startswith("opentelemetry-exporter-otlp-proto-http>=1.44.0,<2") for item in otel
    )

    index = (DOCS / "README.md").read_text(encoding="utf-8")
    observability = (DOCS / "observability.md").read_text(encoding="utf-8")
    cli = (DOCS / "cli.md").read_text(encoding="utf-8")
    assert "observability.md" in index
    assert 'pip install "agnoclaw[otel]"' in observability
    assert "runtime:run:inspect" in observability
    assert "agnoclaw inspect run" in observability
    assert "agnoclaw inspect run" in cli


def test_learning_reconciliation_docs_use_the_exact_agno_observer() -> None:
    candidates = (DOCS / "learning-candidates.md").read_text(encoding="utf-8")
    compatibility = (DOCS / "compatibility.md").read_text(encoding="utf-8")

    assert "VectorDb.name_exists" in candidates
    assert "never calls semantic search" in candidates
    assert "observe_learning_reconciliation_page(" in candidates
    assert "build_learning_reconciliation_worker(" in candidates
    assert "database clock" in candidates
    assert "monotonic fence" in candidates
    assert "Agno 2.6.4/2.9.0" in compatibility


def test_self_improvement_docs_use_the_public_agno_subject_adapter() -> None:
    evaluation = (DOCS / "self-improvement-evaluation.md").read_text(encoding="utf-8")
    migration = (DOCS / "migration-0.12.md").read_text(encoding="utf-8")

    assert "agno_evaluation_subject_factory(" in evaluation
    assert "deterministic and independently controlled" in evaluation
    assert "does not invoke Agno's model-judge internals" in evaluation
    assert "EvaluationCorpusManifest(" in evaluation
    assert "evaluation_corpus_case_set_digest(" in evaluation
    assert "require_governed_corpus=True" in evaluation
    assert "Improvement-evaluation runner 1.1" in migration
    assert "Improvement-evaluation runner 1.2" in migration
    assert "process_evaluation_subject_factory()" in evaluation
    assert "run_process_evaluation_worker" in evaluation
    assert "not a security sandbox" in evaluation
    assert "governed_corpus_required" in migration


def test_self_improvement_docs_define_the_strict_docker_boundary() -> None:
    evaluation = (DOCS / "self-improvement-evaluation.md").read_text(encoding="utf-8")
    security = (DOCS / "security.md").read_text(encoding="utf-8")
    migration = (DOCS / "migration-0.12.md").read_text(encoding="utf-8")

    assert "docker_evaluation_subject_factory(" in evaluation
    assert "DockerEvaluationPolicy(" in evaluation
    assert "scripts/docker_evaluation_probe.py" in evaluation
    assert "https://docs.docker.com/engine/security/seccomp/" in evaluation
    assert "strict Linux-container boundary" in evaluation
    assert "malicious image supply chain" in evaluation
    assert "exact owner label" in security
    assert "credential broker/egress proxy" in security
    assert "Strict Docker evaluation subject" in migration
    assert "No automatic conversion" in migration


def test_recovery_docs_define_the_real_process_sqlite_crash_gate() -> None:
    evaluation = (DOCS / "evaluation.md").read_text(encoding="utf-8")
    operations = (DOCS / "operations-and-recovery.md").read_text(encoding="utf-8")

    command = "scripts/sqlite_runtime_crash_probe.py"
    assert command in evaluation
    assert command in operations
    assert "--allow-process-crash" in evaluation
    assert "50/50" in operations
    assert "actual ungraceful child exits" in operations
    assert "not evidence that a provider effect" in operations


def test_recovery_docs_define_the_capability_effect_crash_gate_honestly() -> None:
    evaluation = (DOCS / "evaluation.md").read_text(encoding="utf-8")
    operations = (DOCS / "operations-and-recovery.md").read_text(encoding="utf-8")
    progress = (DOCS / "releases" / "v0.12.0-progress.md").read_text(encoding="utf-8")

    command = "scripts/operation_effect_crash_probe.py"
    for content in (evaluation, operations):
        prose = " ".join(content.split())
        assert command in content
        assert "--allow-process-crash" in content
        assert "zero duplicate external effects" in prose
        assert "zero blind ambiguous redispatches" in prose
        assert "full Agno" in prose or "arbitrary Agno" in prose
        assert "live provider" in prose or "live SaaS-provider" in prose
    assert "Capability-effect process-crash gate" in progress
    assert "Eight scenarios pass" in progress
    assert "tests/test_operation_effect_crash_probe.py" in (
        ROOT / ".github" / "workflows" / "ci.yml"
    ).read_text(encoding="utf-8")


def test_recovery_docs_define_the_full_agno_stack_restart_gate_honestly() -> None:
    evaluation = (DOCS / "evaluation.md").read_text(encoding="utf-8")
    operations = (DOCS / "operations-and-recovery.md").read_text(encoding="utf-8")
    progress = (DOCS / "releases" / "v0.12.0-progress.md").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    command = "scripts/agno_stack_restart_probe.py"
    for content in (evaluation, operations):
        prose = " ".join(content.split())
        assert command in content
        assert "--allow-process-crash" in content
        assert "planned" in prose
        assert "waiting_for_reconciliation" in prose or "reconciliation" in prose
        assert "zero duplicate" in prose
        assert "tool-batch" in prose or "native tool" in prose
        assert "multi-host" in prose or "multiple hosts" in prose
        assert "recover_pending_runs()" in prose
        assert "owner-scoped" in prose
    assert "AgentHarness/Agno process-restart gate" in progress
    assert "Four public-factory scenarios pass" in progress
    assert "RUN_RECOVERY_SPEC_MISMATCH" in progress
    assert "five-of-five clean-recovery closures" in progress
    assert "tests/test_agno_stack_restart_probe.py" in workflow
    assert "-m integration" in workflow
    assert "factory-backed outer and learning process restart" in workflow
    assert "agno==${{ matrix.agno-version }}" in workflow


def test_recovery_docs_define_tool_checkpoint_and_approval_restart_gates() -> None:
    evaluation = (DOCS / "evaluation.md").read_text(encoding="utf-8")
    operations = (DOCS / "operations-and-recovery.md").read_text(encoding="utf-8")
    compatibility = (DOCS / "compatibility.md").read_text(encoding="utf-8")
    progress = (DOCS / "releases" / "v0.12.0-progress.md").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    tool_command = "scripts/agno_tool_checkpoint_restart_probe.py"
    approval_command = "scripts/agno_approval_restart_probe.py"
    for content in (evaluation, operations):
        prose = " ".join(content.split())
        assert tool_command in content
        assert approval_command in content
        assert "tool-batch" in prose
        assert "six provider calls" in prose
        assert "one reconciliation" in prose or "one `waiting_for_reconciliation`" in prose
        assert "one approved record" in prose
        assert "zero duplicate" in prose
        assert "Agno 2.6.4" in prose
        assert "parser/output-model" in prose
        assert "public-factory model" in prose
        assert "run-owned, isolated, and recreatable" in prose

    assert "AgnoFeature.TOOL_BATCH_CHECKPOINT" in compatibility
    assert "Agno tool-checkpoint process-restart gate" in progress
    assert "Durable approval process-restart gate" in progress
    assert "tests/test_agno_tool_checkpoint_restart_probe.py" in workflow
    assert "tests/test_agno_approval_restart_probe.py" in workflow


def test_learning_docs_define_real_process_reconciliation_worker_restart() -> None:
    learning = (DOCS / "learning.md").read_text(encoding="utf-8")
    candidates = (DOCS / "learning-candidates.md").read_text(encoding="utf-8")
    evaluation = (DOCS / "evaluation.md").read_text(encoding="utf-8")
    progress = (DOCS / "releases" / "v0.12.0-progress.md").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    command = "scripts/learning_reconciliation_restart_probe.py"
    for content in (learning, candidates, evaluation, progress):
        prose = " ".join(content.split())
        assert "process-death" in prose or "process death" in prose
        assert "fence 1→2" in prose
        assert "zero promotion redispatch" in prose
    for content in (candidates, evaluation):
        assert command in content
        assert "--allow-process-crash" in content
        assert "not PostgreSQL" in content
        assert "multi-host" in content
    assert "tests/test_learning_reconciliation_restart_probe.py" in workflow


def test_learning_docs_define_the_live_no_learning_control_gate() -> None:
    evaluation = (DOCS / "evaluation.md").read_text(encoding="utf-8")
    learning = (DOCS / "learning.md").read_text(encoding="utf-8")
    compatibility = (DOCS / "compatibility.md").read_text(encoding="utf-8")

    for content in (evaluation, learning):
        assert "scripts/learning_benefit_probe.py" in content
        assert "--allow-live-model" in content
        assert "<relevant_learnings>" in content
        assert "no-learning" in content
    assert "--allow-remote-ollama" in evaluation
    assert "--evidence-dir" in evaluation
    assert "uv run --isolated --extra local --extra rag" in evaluation
    assert "6/6" in evaluation
    assert "Three consecutive" in evaluation
    assert "one exact local model" in evaluation
    assert "previous-version" in compatibility


def test_operations_docs_define_model_transport_ownership() -> None:
    operations = (DOCS / "operations-and-recovery.md").read_text(encoding="utf-8")
    compatibility = (DOCS / "compatibility.md").read_text(encoding="utf-8")

    assert "Model transport ownership" in operations
    assert "caller-injected Agno Model remains caller-owned" in operations
    assert "AgnoEvaluationSubject(close_agent=True)" in operations
    assert "Ollama's currently non-forwarded HTTPX transport" in compatibility
    assert "ResourceWarning" in compatibility
