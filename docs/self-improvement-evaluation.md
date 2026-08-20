# Evidence-gated harness self-improvement

Status: preview executable evaluation contract

Last reviewed: 2026-08-17

agnoclaw can represent, execute, and evaluate a bounded harness-improvement experiment
without giving the proposer authority to edit the evaluator or promote itself. The
contract is small on purpose: immutable manifests, causal failure clusters, a
falsifiable change hypothesis, one fresh-resource paired runner, three evaluation
slices, one fail-closed gate, and a Pareto helper.

This is an evaluation foundation, not an autonomous source-code optimizer. It executes
caller-defined subjects—including fresh Agno `Agent` subjects through a narrow public
adapter—and an independent verifier, but it does not generate or apply edits, prove
container/VM/kernel sandbox isolation, select benchmark cases, record a ledger decision,
or promote a candidate. A first-party local process boundary now isolates rollout
memory, crashes, environment, working-directory state, and process lifetime. The trusted
host still defines the subjects and verifier, retains their canonical digests, and owns
the operator decision boundary.

## Why this boundary exists

Self-improvement claims are unusually easy to game. A proposer can appear better by
changing the test, judge, model, permissions, budget, dataset, or sample population. It
can overfit the failures it saw, collapse into near-duplicate variants, or trade safety
and cost for one headline score. Reflection text is also not outcome evidence.

The implemented contract follows the acceptance rules derived from
[Lilian Weng's July 2026 harness research](lilian-weng-harness-audit.md):

- diagnose verifier-grounded causal mechanisms rather than terminal labels;
- make the editable component and exact paths inspectable;
- separate generator/reflector/curator/evaluator/operator roles;
- freeze model configuration, evaluator, permission boundary, datasets, verifiers, and
  resource budget;
- require paired held-in, untouched held-out, and cross-task transfer evidence;
- make safety and privacy absolute gates;
- preserve quality, safety, cost, latency, and complexity as separate objectives;
- audit model-judge order and disagreement;
- treat heuristic-only feedback as inconclusive;
- retain non-dominated trade-offs without hiding them behind one scalar score.

## End-to-end authority flow

```text
completed runs + immutable outcome artifacts
                 |
                 v
       verifier-grounded FailureCluster
                 |
component manifest + ChangeHypothesis -------- frozen control plane
                 |                              model / evaluator / permissions
                 |                              datasets / verifiers / budget
                 v
   fresh-resource paired experiment
                 |
       baseline + candidate results
       held-in / held-out / transfer
                 |
                 v
      ImprovementEvaluationGate
         | rejected/inconclusive -> retain evidence
         v qualified
        CandidateEvaluation
                 |
       scoped LearningLedger
                 |
        explicit operator promotion
```

`ImprovementEvaluationGate` is pure: it cannot write a file, call a model, update a
ledger, or promote a learning. `AgentHarness.record_learning_candidate_evaluation()`
is host-only and reauthorizes the candidate's exact tenant/learning namespace before
the ledger mutation.

`ImprovementEvaluationRunner` is separate from that gate. It can call only the supplied
baseline/candidate factories, verifier, and `ArtifactStore`; its result has no mutation
or promotion method. This separation makes a negative experiment as durable and
inspectable as a positive one without turning evaluation into authority.

## Component and diagnosis records

`HarnessComponentManifest` supports exactly seven observable component classes:

| Class | Examples |
|---|---|
| `system_prompt` | base instructions and prompt sections |
| `tool_description` | model-visible schemas and guidance |
| `tool_implementation` | governed effect implementation |
| `middleware` | policy, context, recovery, or routing hooks |
| `skill` | versioned skill instructions and resources |
| `subagent_configuration` | role, model, tools, and delegation bounds |
| `long_term_memory` | governed durable context content/mechanism |

The manifest includes an implementation digest, normalized workspace-relative editable
paths, and a rollback reference. Absolute paths, `..`, workspace root (`.`), duplicate
paths, and non-normalized paths fail construction. Runtime identity, policy,
permissions, secrets, raw trajectories, benchmarks, evaluators, models, and budgets are
control-plane inputs—not component classes the candidate can quietly edit.

`FailureCluster` binds a causal mechanism to a verifier digest, failing run IDs,
evidence artifacts, terminal labels, and a mechanism version. Repeating a terminal
label such as `tool_error` as the causal mechanism is rejected. The verifier and
evidence, not prose confidence, make the diagnosis auditable.

`ChangeHypothesis` then binds:

- exact component IDs and manifest digests;
- exact failure-cluster IDs and digests;
- root-cause inference and evidence artifacts;
- bounded edit paths;
- predicted fixes, regression risks, and passing behavior to preserve;
- prior attempts;
- frozen model, evaluator, and permission digests;
- distinct proposer and evaluator identity digests;
- rollout/token/wall-time/cost budget;
- rollback target, proposer role, and mechanism version.

Call `verify_manifests()` and `verify_failure_clusters()` before executing the
experiment. The evaluation gate repeats both checks, so a caller cannot accidentally
accept a report against different inputs.

## Governed corpus and leakage boundary

Qualification now requires governed-corpus evidence by default. An
`EvaluationCorpusManifest` is content-free: it freezes the ordered case ID, split, task
class, payload digest, semantic-lineage digest, source-artifact ID, and exposure class,
without publishing case payloads. It also binds the corpus/version, curator identity,
selection policy, sampling seed, sealed-case access policy, and decontamination method.

The constructor fails closed when:

- a case ID or exact payload is duplicated;
- held-in, held-out, or transfer is absent;
- one semantic lineage crosses split boundaries;
- a held-in case is not development-visible;
- a held-out or transfer case is not marked sealed; or
- the candidate proposer is also the corpus curator.

Before creating a subject, the runner verifies the runtime cases against the manifest's
exact order and payload digests. It then loads every exact-scope source-provenance
artifact and the decontamination artifact through `ArtifactStore`. Source records must
bind a source digest, authorized usage basis (`license`, `consent`,
`internal_authorized`, or `public_domain`), and retention-policy digest. The
decontamination record must bind the ordered case-set/method/curator, enumerate the
comparison-corpus digests, cover every case, and contain no known or unresolved case
overlaps. Any mismatch or contamination fails before model or tool construction.

This is the minimal staging shape:

```python
from agnoclaw import (
    EvaluationCaseExposure,
    EvaluationCorpusEntry,
    EvaluationCorpusManifest,
    EvaluationSlice,
    evaluation_corpus_case_set_digest,
)

source = await artifact_store.stage_json(
    {
        "type": "agnoclaw.evaluation_corpus_source",
        "schema_version": "1.0",
        "source_id": "reviewed-production-failures-2026q3",
        "source_digest": source_snapshot_digest,
        "usage_basis": "internal_authorized",
        "retention_policy_digest": retention_policy_digest,
    },
    scope=artifact_scope,
    purpose="evaluation_corpus_source",
)
entries = tuple(
    EvaluationCorpusEntry.from_case(
        case,
        lineage_digest=semantic_lineage_digest_by_case[case.case_id],
        source_artifact_id=source.artifact_id,
        exposure=(
            EvaluationCaseExposure.DEVELOPMENT
            if case.slice is EvaluationSlice.HELD_IN
            else EvaluationCaseExposure.SEALED
        ),
    )
    for case in frozen_cases
)
decontamination = await artifact_store.stage_json(
    {
        "type": "agnoclaw.evaluation_corpus_decontamination",
        "schema_version": "1.0",
        "case_set_digest": evaluation_corpus_case_set_digest(entries),
        "method_digest": decontamination_method_digest,
        "checked_case_count": len(entries),
        "reviewer_identity_digest": curator_service_principal_digest,
        "comparison_corpus_digests": comparison_corpus_digests,
        "known_overlap_case_ids": [],
        "unresolved_case_ids": [],
    },
    scope=artifact_scope,
    purpose="evaluation_corpus_decontamination",
)
corpus = EvaluationCorpusManifest(
    corpus_id="recovery-safety",
    version="2026q3.1",
    entries=entries,
    selection_policy_digest=selection_policy_digest,
    sampling_seed_digest=sampling_seed_digest,
    sealed_access_policy_digest=sealed_access_policy_digest,
    decontamination_method_digest=decontamination_method_digest,
    decontamination_artifact_id=decontamination.artifact_id,
    curator_identity_digest=curator_service_principal_digest,
)
```

Pass `corpus_manifest=corpus` and include `source` plus `decontamination` in
`upstream_artifacts`. Their IDs enter the report, gate decision, candidate evaluation,
runner digest, and per-case evidence chain. A development experiment may omit the
manifest, but `EvaluationGatePolicy(require_governed_corpus=True)` is the default and
adds `governed_corpus_required`, preventing qualification. The explicit
`require_governed_corpus=False` setting exists only for legacy/manual compatibility and
must not be described as corpus-governed evidence.

The manifest cannot prove that an embedding application really withheld raw sealed
cases before constructing it, that a provider never trained on similar data, or that a
lineage algorithm catches every semantic near-duplicate. Enforce the bound sealed-access
policy in an external ACL/sandbox, retain its audit evidence, use independently curated
comparison corpora, and treat unknown model-training contamination as a limitation.

## Executable paired runner

`ImprovementEvaluationRunner` is the first-party evidence producer. Its public inputs
are deliberately plain:

- exact `ArtifactReference` values for every hypothesis, failure-cluster, and optional
  judge-calibration and corpus evidence ID;
- immutable `EvaluationCase` values assigned to held-in, held-out, or transfer;
- zero-argument baseline and candidate factories that create a fresh async callable for
  each rollout;
- optional paired subject-contract digests exposed by first-party process factories;
- canonical baseline and candidate digests;
- one independently controlled verifier returning `EvaluationScore`;
- novelty, diversity, complexity, feedback-strength, and optional judge-audit records.

`corpus_manifest` is optional for development execution but required by the default
qualification gate as described above.

The runner checks manifest and failure-cluster digests before execution, then loads every
required upstream artifact through `ArtifactStore`. The supplied set must match exactly,
and each reference must have the runner's exact tenant/user/run scope. Loading verifies
the content address, stored checksum, plaintext checksum, JSON encoding, and configured
encryption boundary. A missing, extra, corrupt, or differently scoped reference fails
before either subject is created.

For every case the runner:

1. creates a new baseline resource and a new candidate resource;
2. awaits optional `asetup()`, the async rollout, and optional `aclose()`;
3. alternates baseline-first and candidate-first execution order across cases;
4. applies the same frozen verifier to each successful result;
5. converts an exception/timeout from the subject's awaited task execution into explicit
   zero-quality, zero-safety negative evidence without exposing the exception message;
6. invalidates the experiment if factory/setup/result/cleanup lifecycle contracts fail,
   if cleanup cannot prove fresh-resource isolation, or if the verifier fails/violates
   its return contract;
7. stages one scoped, content-addressed `improvement_evaluation_case` JSON artifact with
   case, order, subject-contract digests, outputs, scores, usage, latency, and safe error
   type.

It preflights the two-rollouts-per-case count and enforces rollout, token, cost, and wall
budgets as observations arrive. A budget overrun invalidates the experiment; partially
staged content is uncommitted artifact data eligible for normal garbage collection.
Case count, per-case canonical JSON (256 KiB by default), and total case JSON (16 MiB by
default) are independently bounded and included in the runner digest; both byte limits
are configurable only within hard ceilings.
Per-rollout timeouts cover async setup and subject execution. Cleanup receives its own
bounded await. Sync factories and sync verifiers are trusted host callbacks and must be
short/non-blocking; subject execution itself must return an awaitable.

The following is the minimal shape (the manifest, failure cluster, hypothesis, and
upstream references are omitted only for space):

```python
from agnoclaw import (
    ArtifactScope,
    EvaluationCase,
    EvaluationRollout,
    EvaluationScore,
    EvaluationSlice,
    ImprovementEvaluationGate,
    ImprovementEvaluationRunner,
)


class Subject:
    async def __call__(self, case: EvaluationCase) -> EvaluationRollout:
        output = await execute_frozen_variant(case.payload)
        return EvaluationRollout(output=output, tokens=output.tokens, cost_usd=output.cost)

    async def aclose(self) -> None:
        await close_owned_resources()


def baseline_factory() -> Subject:
    return Subject()  # a new resource for this rollout


def candidate_factory() -> Subject:
    return Subject()  # construct the candidate variant here


def verifier(case: EvaluationCase, rollout: EvaluationRollout) -> EvaluationScore:
    result = deterministic_verifier(case.payload, rollout.output)
    return EvaluationScore(
        quality=result.quality,
        safety=result.safety,
        safety_passed=result.safety_passed,
        privacy_passed=result.privacy_passed,
        objective=True,
    )


runner = ImprovementEvaluationRunner(
    artifact_store,
    artifact_scope=ArtifactScope(
        tenant_id="tenant-1",
        user_id="operator-1",
        run_id="experiment-2026-08-09",
    ),
    evaluator_identity_digest=evaluator_service_principal_digest,
    per_rollout_timeout=120,
)
run = await runner.run(
    candidate_id="candidate-retry-v2",
    hypothesis=hypothesis,
    manifests=(manifest,),
    failure_clusters=(cluster,),
    cases=frozen_cases,
    baseline_factory=baseline_factory,
    candidate_factory=candidate_factory,
    baseline_digest=baseline_implementation_digest,
    candidate_digest=candidate_implementation_digest,
    verifier=verifier,
    upstream_artifacts=(*verified_references, source, decontamination),
    novelty_score=0.7,
    diversity_score=0.8,
    added_complexity=0.1,
    corpus_manifest=corpus,
)
decision = ImprovementEvaluationGate().evaluate(
    run.report,
    hypothesis=hypothesis,
    manifests=(manifest,),
    failure_clusters=(cluster,),
)
```

### Fresh-process subject adapter

Use `process_evaluation_subject_factory()` when a candidate must not share Python state,
environment, working-directory state, or crash lifetime with the evaluator. It launches
one new child for one rollout with `create_subprocess_exec`—never a shell—and exchanges
one exact JSON request/response over bounded stdin/stdout. The executable must be an
absolute path. Parent environment variables are not inherited; pass only explicitly
reviewed entries. With no `working_directory`, every subject receives a fresh temporary
directory that must be deleted successfully during `aclose()`.

The worker side is intentionally tiny:

```python
# /absolute/path/evaluation_worker.py
import sys

from agnoclaw import EvaluationRollout, run_process_evaluation_worker


async def execute(case):
    result = await execute_frozen_variant(case.payload, variant=sys.argv[1])
    return EvaluationRollout(
        output=result.public_output,
        tokens=result.tokens,
        cost_usd=result.cost_usd,
    )


raise SystemExit(run_process_evaluation_worker(execute))
```

Bind both sides of the pair to the same isolation class:

```python
import sys

from agnoclaw import process_evaluation_subject_factory

worker = "/absolute/path/evaluation_worker.py"
baseline_factory = process_evaluation_subject_factory(
    (sys.executable, worker, "baseline"),
    environment={"PROVIDER_API_KEY": provider_api_key},
)
candidate_factory = process_evaluation_subject_factory(
    (sys.executable, worker, "candidate"),
    environment={"PROVIDER_API_KEY": provider_api_key},
)

run = await runner.run(
    # immutable manifests/cases/evidence omitted here
    baseline_factory=baseline_factory,
    candidate_factory=candidate_factory,
    baseline_digest=baseline_implementation_digest,
    candidate_digest=candidate_implementation_digest,
)
```

Runner schema 1.2 records `baseline_subject_contract_digest` and
`candidate_subject_contract_digest` in the report, runner digest, and every case
artifact. Each command digest binds its exact argv to a separate
`subject_isolation_digest`; the shared isolation digest binds protocol,
environment-value digests, working-directory mode, I/O limits, termination grace, and
process-group policy without exposing environment values. A bound factory on only one
side, or two different isolation digests, is rejected as an incomparable experiment.
Factory and subject reprs expose only the executable basename and contract digest.

Requests default to 1 MiB; stdout to 1 MiB; stderr to 64 KiB. All limits have fixed hard
ceilings. Child stderr and malformed response bodies never enter evidence or exception
messages. Cancellation, timeout, stream overflow, and protocol failure terminate and
reap the child; POSIX runs use a fresh process group and adversarial tests prove a
spawned descendant is reaped too. Cleanup failure invalidates the experiment rather
than presenting isolation as successful.

This is strong local process/state/fault isolation, not a security sandbox. The child
runs as the same OS identity and can still address filesystem paths or networks allowed
to that identity. Use the strict Docker subject below when the evaluator must not inherit
host filesystem, environment, network, or process authority. Windows process-tree
containment is not yet a certified release lane.

### Strict Docker evaluation subjects

`docker_evaluation_subject_factory()` preserves the same JSON worker protocol while
placing every rollout in a new, automatically removed Linux container:

```python
from agnoclaw import DockerEvaluationPolicy, docker_evaluation_subject_factory

policy = DockerEvaluationPolicy(
    image="registry.example/evaluator@sha256:<64-hex-digest>",
    platform="linux/amd64",
    memory_bytes=512 * 1024 * 1024,
    cpu_limit=1.0,
    pids_limit=64,
)
baseline_factory = docker_evaluation_subject_factory(
    "/usr/bin/docker", policy, ("python3", "/app/worker.py", "baseline")
)
candidate_factory = docker_evaluation_subject_factory(
    "/usr/bin/docker", policy, ("python3", "/app/worker.py", "candidate")
)
```

The policy accepts only a full image ID or repository digest and an exact
`linux/amd64` or `linux/arm64` platform. Image inspection fails before model work if the
resolved OS/architecture differs or the image declares a writable `VOLUME`. The
factory overrides the image entrypoint and healthcheck, never pulls, injects no host
environment, creates no mount, disables networking, makes the root filesystem
read-only, and provides only a bounded `noexec,nosuid,nodev` `/tmp`. It runs as numeric
UID/GID `65532:65532` by default, drops every capability, sets `no-new-privileges`,
forces Docker's built-in seccomp profile, and bounds CPU, memory/swap, PIDs, open files,
core dumps, protocol streams, and wall time. The subject verifies its exact owner label
before forced cleanup and refuses to remove a differently owned container.

Baseline and candidate must use the same image, platform, and policy; that shared
isolation digest is bound separately from each exact container command. Validate the
deployment daemon with one explicitly authorized temporary container:

```bash
uv run python scripts/docker_evaluation_probe.py \
  --docker /absolute/path/docker \
  --image 'registry.example/evaluator@sha256:<64-hex-digest>' \
  --platform linux/amd64 \
  --allow-live-docker
```

This is a strict Linux-container boundary, not a VM or a defense against a compromised
Docker daemon, host kernel, or malicious image supply chain. Prefer a rootless daemon
where available. Provider-backed evaluation that needs network access or credentials
must use a separately audited egress proxy/credential broker or stronger external VM;
this no-network profile deliberately provides neither. See Docker's official
[run reference](https://docs.docker.com/reference/cli/docker/container/run) and
[seccomp reference](https://docs.docker.com/engine/security/seccomp/).

### Agno model subject adapter

`agno_evaluation_subject_factory()` is the supported bridge from the paired runner to
Agno model execution. The host supplies a zero-argument factory; agnoclaw calls it once
per rollout and adapts the returned `Agent.arun()` result to `EvaluationRollout`. This
keeps model/provider selection in the embedding application and adds no provider SDK to
the core wheel.

```python
from agno.agent import Agent

from agnoclaw import agno_evaluation_subject_factory


def build_baseline_agent() -> Agent:
    return Agent(
        model=build_fresh_baseline_model(),
        instructions=frozen_baseline_instructions,
        tools=build_fresh_evaluation_tools(),
    )


def build_candidate_agent() -> Agent:
    return Agent(
        model=build_fresh_candidate_model(),
        instructions=frozen_candidate_instructions,
        tools=build_fresh_evaluation_tools(),
    )


run = await runner.run(
    candidate_id="candidate-retry-v2",
    hypothesis=hypothesis,
    manifests=(manifest,),
    failure_clusters=(cluster,),
    cases=frozen_cases,
    baseline_factory=agno_evaluation_subject_factory(build_baseline_agent),
    candidate_factory=agno_evaluation_subject_factory(build_candidate_agent),
    baseline_digest=baseline_implementation_digest,
    candidate_digest=candidate_implementation_digest,
    verifier=verifier,  # deterministic and independently controlled
    upstream_artifacts=(*verified_references, source, decontamination),
    novelty_score=0.7,
    diversity_score=0.8,
    added_complexity=0.1,
    corpus_manifest=corpus,
)
```

The default input is the raw case payload when it is a string and canonical JSON
otherwise. A synchronous `input_builder(case)` may produce a provider-specific input.
The default output retains only JSON-like `RunOutput.content`; Pydantic content is
converted with `model_dump(mode="json")`. A synchronous `output_builder(content)` may
normalize another public content shape. Provider-private messages, reasoning, metadata,
and exception text are not copied into the rollout. Public `RunMetrics.total_tokens`
(or input plus output tokens) and `cost` feed the runner's frozen budgets; malformed or
negative metrics fail closed.

Each call receives a new opaque Agno session ID containing a digest of the case ID, not
the raw ID. The factory must actually return a fresh `Agent` and fresh stateful tools,
models, and stores; agnoclaw can prove that it called the factory, not that the factory
did not reuse globals. Set `close_agent=True` only when the supplied Agent owns a
meaningful `aclose()` or `close()` lifecycle. Provider credentials, caches, network
namespaces, and database namespaces remain host-owned isolation concerns.

This adapter deliberately does not invoke Agno's model-judge internals, choose a scorer,
or convert a model judgment into promotion authority. A model judge may contribute a
separately calibrated `JudgeAudit`, but it cannot waive the deterministic safety,
privacy, provenance, resource, or held-out/transfer gates. The adapter contract is
tested on the supported Agno 2.6.4 and 2.9.0 lanes through the public `Agent.arun`,
`RunOutput.content`, and `RunMetrics` surfaces.

`run.upstream_artifacts`, `run.case_artifacts`, and `run.evidence_artifacts` preserve
exact references for retention/ledger integration. `execution_order_balanced` means the
two subject execution positions differ by no more than one case. It is not a substitute
for `JudgeAudit.balanced_order`: a model judge must independently randomize/balance the
presentation it sees and retain calibration evidence.

`runner_digest` binds the schema, the installed runner module's actual SHA-256 digest,
candidate/hypothesis/baseline/evaluator identities, artifact scope, timeout and case
bounds, feedback/judge controls, optional paired subject-contract plus shared isolation
digests, and exact
ordered case bodies. Every per-case artifact
also exposes `runner_implementation_digest`, so an audit can bind the report to the
installed code artifact rather than trusting a semantic version alone.

### Statistical evidence

Each runner report contains one `PairedQualityStatistic` per slice. It computes the
candidate-minus-baseline quality delta on the same case, then records mean delta,
wins/ties/losses, and a two-sided 95% paired Student-t confidence interval. Zero-variance
pairs produce an exact interval; one-case slices deliberately produce `[-1, 1]` and
cannot establish benefit. Bounds are clipped to the possible score-delta range.

When paired statistics are present, `runner_digest` is mandatory and the gate requires
all three slices and exact sample-count agreement. The confidence lower bound must meet
the held-in gain threshold and may not cross the allowed held-out or transfer regression
boundary. Aggregate means can therefore look acceptable while uncertainty still causes
rejection. Manually assembled legacy reports remain supported without statistics, but
they must not be described as first-party-runner-certified.

The fixed paired-t method is intentionally inspectable and dependency-free, but it
assumes independent case pairs and an adequately representative delta distribution. It
does not provide sequential-testing, multiple-comparison, non-parametric, power-analysis,
or model-judge calibration guarantees. Production claims need a pre-registered sampling
plan and a domain-appropriate independent statistical audit in addition to this preview
gate.

### Isolation and evidence cautions

“Fresh resource” means the factory is called once per rollout and its owned lifecycle is
closed before the next verifier result is accepted. It does **not** mean a separate OS
process, container, VM, network namespace, credential set, or provider account. A
factory can still return a wrapper around shared global state. Use externally isolated
workers/sandboxes when contamination, hostile code, secrets, or provider-side caches are
in scope; keep their identity in the baseline/candidate digests and evidence.

Case payloads and subject outputs may be sensitive and are deliberately preserved for
reproducibility. Use an encrypted tenant-scoped `ArtifactStore`, least-privilege access,
an explicit retention/deletion policy, and privacy-aware payload shaping. The runner
does not redact arbitrary domain data for the host.

## Manual evaluation boundary

The following compatibility example shows how a trusted host can construct aggregate
records directly. Real evidence IDs should be verified `ArtifactStore` references; use
the executable runner above when claiming first-party paired evidence.

```python
from agnoclaw import (
    ChangeBudget,
    ChangeHypothesis,
    EvaluationResourceUsage,
    EvaluationSlice,
    EvaluationSliceResult,
    FailureCluster,
    FeedbackStrength,
    HarnessComponentClass,
    HarnessComponentManifest,
    ImprovementEvaluation,
    ImprovementEvaluationGate,
    ImprovementRole,
    PromotionActor,
)


def sha(character: str) -> str:
    return "sha256:" + character * 64


manifest = HarnessComponentManifest(
    component_id="retry-instructions",
    component_class=HarnessComponentClass.SYSTEM_PROMPT,
    version="12",
    implementation_digest=sha("a"),
    editable_paths=("src/agnoclaw/prompts/retry.md",),
    rollback_reference="git:baseline",
    description="Effect-aware retry instructions.",
)
cluster = FailureCluster(
    cluster_id="unsafe-retry-classification",
    causal_mechanism="missing non-repeatable effect classification",
    verifier_digest=sha("b"),
    failure_run_ids=("run-1", "run-2"),
    evidence_artifact_ids=("artifact-diagnosis",),
    terminal_labels=("tool_error",),
    mechanism_version="clusterer:v1",
)
hypothesis = ChangeHypothesis(
    change_id="change-retry-instructions-v1",
    target_component_ids=(manifest.component_id,),
    component_manifest_digests=(manifest.digest,),
    failure_cluster_ids=(cluster.cluster_id,),
    failure_cluster_digests=(cluster.digest,),
    evidence_artifact_ids=("artifact-hypothesis",),
    inferred_root_cause="Retry instructions omit effect classes.",
    bounded_edit_surface=manifest.editable_paths,
    predicted_fixes=("classify an effect before retry",),
    at_risk_regressions=("unnecessary refusal",),
    behaviors_to_preserve=("never replay an unknown non-repeatable effect",),
    previous_attempt_ids=(),
    model_config_digest=sha("c"),
    evaluator_digest=sha("d"),
    permission_digest=sha("e"),
    proposer_identity_digest=sha("6"),
    budget=ChangeBudget(
        max_rollouts=100,
        max_tokens=100_000,
        max_wall_seconds=3_600,
        max_cost_usd=20,
    ),
    rollback_target="git:baseline",
    proposed_by=ImprovementRole.GENERATOR,
    mechanism_version="generator:v1",
)


def result(
    slice_name: EvaluationSlice,
    *,
    quality: float,
    evidence: str,
) -> EvaluationSliceResult:
    samples = 10 if slice_name is EvaluationSlice.TRANSFER else 20
    dataset = {
        EvaluationSlice.HELD_IN: sha("1"),
        EvaluationSlice.HELD_OUT: sha("2"),
        EvaluationSlice.TRANSFER: sha("3"),
    }[slice_name]
    return EvaluationSliceResult(
        slice=slice_name,
        task_class=f"{slice_name.value}-tasks",
        dataset_digest=dataset,
        verifier_digest=sha("f"),
        sample_count=samples,
        quality=quality,
        safety=0.99,
        cost_usd=1.0,
        latency_seconds=2.0,
        objective_fraction=0.9,
        evidence_artifact_ids=(evidence,),
    )


baseline = (
    result(EvaluationSlice.HELD_IN, quality=0.70, evidence="artifact-b-hi"),
    result(EvaluationSlice.HELD_OUT, quality=0.75, evidence="artifact-b-ho"),
    result(EvaluationSlice.TRANSFER, quality=0.70, evidence="artifact-b-xfer"),
)
candidate = (
    result(EvaluationSlice.HELD_IN, quality=0.75, evidence="artifact-c-hi"),
    result(EvaluationSlice.HELD_OUT, quality=0.76, evidence="artifact-c-ho"),
    result(EvaluationSlice.TRANSFER, quality=0.71, evidence="artifact-c-xfer"),
)
report = ImprovementEvaluation(
    candidate_id="lc-retry-v1",
    hypothesis_digest=hypothesis.digest,
    baseline=baseline,
    candidate=candidate,
    evaluator_digest=hypothesis.evaluator_digest,
    evaluator_identity_digest=sha("7"),
    model_config_digest=hypothesis.model_config_digest,
    permission_digest=hypothesis.permission_digest,
    feedback_strength=FeedbackStrength.OBJECTIVE,
    usage=EvaluationResourceUsage(
        rollouts=60,
        tokens=60_000,
        wall_seconds=600,
        cost_usd=6,
    ),
    safety_passed=True,
    privacy_passed=True,
    novelty_score=0.7,
    diversity_score=0.8,
    added_complexity=0.1,
    corpus_manifest_digest=corpus.digest,
    corpus_evidence_artifact_ids=(source.artifact_id, decontamination.artifact_id),
)
decision = ImprovementEvaluationGate().evaluate(
    report,
    hypothesis=hypothesis,
    manifests=(manifest,),
    failure_clusters=(cluster,),
)
evaluation = decision.to_candidate_evaluation(
    evaluation_id="evaluation-retry-v1",
    evaluated_by=PromotionActor.OPERATOR,
)

# `harness` must have an explicit LearningPolicy, ArtifactStore, and LearningLedger.
qualified_record = await harness.record_learning_candidate_evaluation(
    evaluation,
    context=trusted_context,
    mutation_id="evaluation:lc-retry-v1:v1",
)
```

The example reuses identical frozen dataset and verifier digests for baseline and
candidate. A real runner should also preserve the exact case membership and sampling
seed inside the referenced artifacts.

## Default gate policy

`EvaluationGatePolicy` is immutable, serializable with `to_dict()`, and has its own
canonical digest. Defaults are conservative development floors, not universal claims:

| Check | Default |
|---|---:|
| held-in samples | 20 |
| held-out samples | 20 |
| transfer samples | 10 |
| minimum held-in quality gain | 0.01 |
| allowed held-out/transfer quality regression | 0.00 |
| allowed safety regression | 0.00 |
| maximum per-slice cost ratio | 1.50 |
| maximum per-slice latency ratio | 1.50 |
| minimum objective-verifier fraction | 0.50 |
| minimum novelty/diversity score | 0.10 / 0.10 |
| maximum unreviewed judge disagreement | 0.20 |
| governed corpus required | yes |

Programs should version and retain the policy digest with every evaluation. Change a
policy only through normal reviewed configuration change; never let the candidate pick
its threshold after seeing results.

For runner reports, the three quality thresholds apply to confidence lower bounds as
well as aggregate deltas. Direct/manual reports have no confidence claim and receive
only the aggregate checks for backward compatibility.

## Verdict semantics

| Verdict | Meaning |
|---|---|
| `qualified` | Every hard and uncertainty gate passed. This permits reviewed ledger qualification; it does not auto-promote. |
| `rejected` | At least one hard regression, provenance, comparability, safety, privacy, cost, latency, novelty, or diversity gate failed. |
| `inconclusive` | Only weak-feedback or unaudited-judge uncertainty remains. Gather better evidence or require human judgment. |

A hard failure always dominates an inconclusive reason. For example, heuristic-only
feedback plus a held-out regression is `rejected`, not `inconclusive`.

Control-plane drift and budget overrun are stronger than a failed score: the experiment
is invalid and raises a typed `HarnessError`. It cannot be interpreted as a valid
negative result because the comparison contract itself changed.

### Retained negative-result query

After `record_learning_candidate_evaluation()`, rejected and inconclusive decisions are
queryable through the same durable learning ledger rather than a second archive:

```python
from agnoclaw import EvaluationArchiveQuery

page = await harness.query_learning_evaluation_archive(
    context=trusted_context,
    query=EvaluationArchiveQuery(
        reason_code="judge_order_unbalanced",
        evaluator_digest=evaluator_service_principal_digest,
        limit=50,
    ),
)
```

The default query includes only `REJECTED` and `INCONCLUSIVE`. It supports stable gate
reason, evaluator, mechanism, target, and safety filters plus an owner-bound descending
keyset cursor. The projection retains policy, hypothesis, evaluation, runner, and corpus
digests when the gate supplied them. It deliberately omits candidate content, operator
notes, raw metrics, control metrics, and artifact identifiers. This lets operators find
failed hypotheses and repeated failure modes without making private evidence a search
index or giving the model access to rejection history.

The query is host-only and read-only. Finding a prior rejection cannot create, qualify,
promote, or mutate a candidate. SQLite and PostgreSQL share the contract and exact-owner
isolation; custom ledgers opt in through `EvaluationArchiveLedger`. Schema v5 writes a
typed, content-free filter projection and validated reason-code relation atomically
with canonical evaluation JSON. The bounded PostgreSQL 17 gate passes with 10,000
queried-owner and 10,000 noisy-neighbor evaluations, two disjoint keyset pages per
sample, three concurrent noisy workers, 56.64 ms p95, 58.87 ms p99, 0.974x slowdown,
and exact cleanup. Those loopback numbers are regression evidence, not a production
SLA; production-volume, memory, failover/partition, and additional-index policy remain
open.

## Model-judge evidence

When `JudgeAudit.used=True`, the report must include:

- canonical judge model and prompt digests;
- one or more calibration evidence artifacts;
- whether candidate/control presentation order was balanced;
- disagreement rate;
- whether above-threshold disagreements received review.

Unbalanced order or unreviewed excessive disagreement produces `inconclusive` unless a
hard gate also fails. Deterministic verifiers should lead whenever the outcome can be
expressed objectively.

## Pareto archive

`pareto_frontier()` keeps candidates that are not dominated across five independent
dimensions:

- quality and safety: higher is better;
- cost, latency, and added complexity: lower is better.

It intentionally does not accept weights and does not calculate a single “fitness”
number. Duplicate candidate/evaluation identities with conflicting objective vectors
raise `IMPROVEMENT_PARETO_ENTRY_CONFLICT`; silently replacing history would corrupt the
archive. Rejected and dominated records still belong in the durable candidate/evidence
archive even when they are absent from the frontier. The negative-result query above is
the content-free operator read path over that retained ledger truth.

## Failure behavior and security boundary

Important typed failures include:

| Error code | Meaning |
|---|---|
| `IMPROVEMENT_EDIT_SURFACE_INVALID` | path is absolute, root-wide, traversing, duplicate, or non-normalized |
| `IMPROVEMENT_CAUSAL_CLUSTER_INVALID` | diagnosis merely repeats a terminal label |
| `IMPROVEMENT_MANIFEST_*` | target set, cardinality, identity, or digest is inconsistent |
| `IMPROVEMENT_CLUSTER_*` | failure evidence set, cardinality, identity, or digest is inconsistent |
| `IMPROVEMENT_ROLE_CONFLICT` | evaluator role attempted to become the proposer |
| `IMPROVEMENT_EVALUATOR_INDEPENDENCE_REQUIRED` | proposer and evaluator service-principal digests are identical |
| `IMPROVEMENT_HYPOTHESIS_DIGEST_MISMATCH` | report is attached to another hypothesis |
| `IMPROVEMENT_CONTROL_PLANE_DRIFT` | evaluator, model config, or permissions changed |
| `IMPROVEMENT_BUDGET_EXCEEDED` | measured experiment usage exceeded the frozen budget |
| `IMPROVEMENT_JUDGE_PROVENANCE_REQUIRED` | judge evidence lacks frozen provenance/calibration |
| `IMPROVEMENT_CASE_SET_INVALID` | case IDs/slices/task classes are missing, duplicate, or out of bounds |
| `IMPROVEMENT_CORPUS_CASE_MISMATCH` | runtime cases/order/content do not match the frozen content-free manifest |
| `IMPROVEMENT_CORPUS_EXACT_DUPLICATE` | an exact payload occurs more than once |
| `IMPROVEMENT_CORPUS_LINEAGE_LEAKAGE` | one declared semantic lineage crosses splits |
| `IMPROVEMENT_CORPUS_EXPOSURE_INVALID` | development/sealed exposure conflicts with the split |
| `IMPROVEMENT_CORPUS_AUTHORITY_CONFLICT` | candidate proposer also controls corpus curation |
| `IMPROVEMENT_CORPUS_EVIDENCE_INVALID` | source/decontamination evidence is missing, malformed, or drifted |
| `IMPROVEMENT_CORPUS_CONTAMINATION_DETECTED` | decontamination found known or unresolved overlap |
| `IMPROVEMENT_EVIDENCE_SET_MISMATCH` | exact upstream artifact IDs do not match the hypothesis/cluster/judge set |
| `IMPROVEMENT_EVIDENCE_SCOPE_MISMATCH` | an upstream reference belongs to another tenant/user/run scope |
| `IMPROVEMENT_EVALUATOR_FAILED` | frozen verifier raised, timed out, or violated its score contract |
| `IMPROVEMENT_SUBJECT_CONTRACT_INVALID` | factory/setup/result/cleanup contract failed, so resource isolation or comparability is invalid |
| `IMPROVEMENT_AGNO_RESPONSE_INVALID` | an Agno subject returned no public response/content contract |
| `IMPROVEMENT_AGNO_METRICS_INVALID` | token or cost evidence was malformed, non-finite, or negative |
| `IMPROVEMENT_PAIRED_STATISTIC_REQUIRED` | a runner report omitted one of the three paired statistics |
| `IMPROVEMENT_PAIRED_STATISTIC_MISMATCH` | statistic count differs from the frozen case population |
| `IMPROVEMENT_PARETO_ENTRY_CONFLICT` | one evaluation identity maps to conflicting objectives |

The pure gate validates structure, digests, comparisons, and policy; it does not query
the artifact store. The first-party runner performs the exact reference/scope/load checks
before it constructs a report. Hosts constructing reports manually retain that
responsibility. Candidate evaluation then crosses the scoped learning gateway and
transactional ledger; model-facing tools receive no candidate, evaluation, promotion,
rollback, or reconciliation authority.

## Current limits and next certification gates

Implemented and contract-tested:

- immutable seven-class manifests and bounded paths;
- digested causal clusters and change hypotheses;
- role separation and frozen control/budget validation;
- paired dataset/verifier/sample checks across held-in, held-out, and transfer;
- safety, privacy, cost, latency, objective-feedback, novelty, and diversity gates;
- judge provenance/order/disagreement audit;
- deterministic evaluation digest and candidate-ledger conversion;
- five-dimensional Pareto frontier;
- async fresh-resource paired baseline/candidate execution with balanced position;
- exact scoped upstream-artifact verification and per-case content-addressed evidence;
- subject timeout/exception negative evidence and evaluator-failure invalidation;
- live rollout/token/wall/cost budget enforcement;
- dependency-free paired 95% confidence records and confidence-aware gate checks;
- content-free versioned corpus manifests, exact ordered-case binding, split/lineage/
  exposure rules, curator independence, scoped source/retention/usage provenance, and
  fail-before-model decontamination evidence;
- provider-neutral Agno model subjects with fresh factory invocation, opaque sessions,
  JSON-like content projection, bounded token/cost evidence, and optional owned cleanup;
- an opt-in local Agno Learned Knowledge benefit probe with an identical no-learning
  control, exact Ollama model/embedder digests, frozen decoding, objective expected-token
  verification, balanced held-in/out/transfer order, content-free stdout, and strict
  positive-benefit/no-loss qualification across every slice;
- runner-schema-1.2 local process subjects with no shell, empty-by-default environment,
  fresh temporary working directories, bounded JSON/stdout/stderr, redacted immutable
  contract digests, single-use children, timeout/cancellation cleanup, and POSIX
  process-group reaping;
- scoped SQLite/PostgreSQL candidate persistence after host submission.

Not yet implemented or certified:

- container/VM/kernel security isolation, credential brokering, CPU/memory/process hard
  limits, and certified Windows process-tree containment;
- sequential/multiple-comparison/non-parametric statistical policies and power analysis;
- managed corpus registry/search/retention automation, enforced sealed-case ACLs,
  semantic near-duplicate tooling, and model-training-set contamination proof;
- automatic failure mining, candidate generation, code editing, or promotion;
- sandboxed component patch application and rollback executor;
- long-duration model-backed benefit evidence across providers/task classes and against
  previous released versions (the narrow local Learned Knowledge probe proves only one
  exact model/embedder/corpus mechanism smoke);
- certified exact-state reconciliation observers wired to the durable scheduler
  (restart-safe discovery plus a bounded evidence-verifying host coordinator are
  implemented), and full retention/deletion proofs.

Until those gates land, describe this as an executable evidence-gated self-improvement
evaluation contract—not as autonomous self-improvement. The broader release requirements remain in
[Harness evaluation and release gates](evaluation.md) and the
[0.12 implementation progress ledger](releases/v0.12.0-progress.md).
