# Lilian Weng harness and self-improvement research audit

Status: normative research reconciliation for agnoclaw 0.12

Reviewed: 2026-08-08; full-post and live blog-index recheck: 2026-08-17

Live-source recheck on 2026-08-17: the official blog index still lists the July 4, 2026
harness post as Weng's newest article overall and newest harness/self-improvement
article. Its ACE, MCE, Self-Harness, AHE,
Generator/Reflector/Curator, held-out, negative-result, observability, and Pareto ideas
were re-read in the primary post and checked against the normative gates below rather
than inferred from summaries. The audit explicitly covers the Plan → Execute →
Observe/Test → Improve loop, filesystem-backed long-horizon state, inspectable child
jobs, incremental itemized playbooks, the seven AHE component classes, causal failure
mining, preserved failed attempts, and read-only tracer/verifier/model/budget controls.

## Scope

This audit checks the approved 0.12 plan against Lilian Weng's latest harness research
and the earlier posts that supply its memory, reflection, evaluator, and reward-hacking
foundations:

- [Harness Engineering for Self-Improvement](https://lilianweng.github.io/posts/2026-07-04-harness/),
  2026-07-04;
- [Why We Think](https://lilianweng.github.io/posts/2025-05-01-thinking/),
  2025-05-01;
- [Reward Hacking in Reinforcement Learning](https://lilianweng.github.io/posts/2024-11-28-reward-hacking/),
  2024-11-28; and
- [LLM Powered Autonomous Agents](https://lilianweng.github.io/posts/2023-06-23-agent/),
  2023-06-23.

The July 2026 post is the primary source for this reconciliation. The older posts are
used where they sharpen its claims about external feedback, memory retrieval, faithful
reasoning, evaluator bias, and reward tampering.

## Verdict

The approved plan already contains the correct macro-architecture: a deliberately small
OS-like interface; a plan/act/observe/verify loop; durable files, artifacts, and
trajectory; explicit child jobs; bounded context; governed learning candidates;
held-out evaluation; permissions outside the learning loop; and reversible promotion.

The audit found ten places where those ideas needed a more testable contract. They are
now release requirements rather than implied intent.

## Implementation checkpoint

As of this research date, the plan-to-code boundary is explicit:

- durable run state, ordered events, operation intent/settlement, leases, and scoped
  artifacts implement the inspectable workflow/filesystem foundation;
- immutable `CapabilitySpec` and `_HarnessSpec` begin component observability without
  expanding the small user-facing grammar;
- immutable `LearningPolicy` is separate from trusted run-resolved `LearningScope`;
  personal/session writes are bounded and can require consent, while institutional
  direct writes by the proposing model are forbidden;
- each explicit learning run gets an isolated LearningMachine/Agent view with opaque
  tenant/user/session storage keys, rather than a reusable global namespace;
- the fabricated periodic `optimize_memories()` promise was removed because current
  Agno exposes Curator internals but no certified operation with that contract.
- an artifact-backed SQLite/PostgreSQL candidate archive now preserves source runs, evidence,
  mechanism version, scope, confidence, expiry, immutable supersession, evaluations,
  negative states, quarantine, and audit tombstones;
- promotion and rollback intent commit before the external Agno call, ambiguous outcomes
  are fenced from blind replay, and the default adapter is limited to uniquely named
  Learned Knowledge because Entity Memory merges and Decision Log writes do not yet have
  certified inverses;
- restart-safe unknown-effect discovery now feeds a bounded host-observer coordinator;
  observations bind to exact candidate revisions, immutable evidence bytes and owner
  scope are verified, and concurrent workers converge through ledger CAS without
  replaying the effect;
- preview `HarnessComponentManifest`, `FailureCluster`, and `ChangeHypothesis` records
  now bind the seven component classes, normalized edit paths, causal evidence, exact
  manifest/cluster digests, separate proposer role, frozen model/evaluator/permission
  controls, experiment budget, prediction, preservation risks, and rollback target;
- `ImprovementEvaluationGate` now checks paired held-in, untouched held-out, and frozen
  transfer dataset/verifier/sample identity; rejects safety, privacy, cost, latency,
  novelty, diversity, and hard-regression failures; audits judge provenance/order/
  disagreement; and emits an immutable candidate-ledger evaluation;
- `ImprovementEvaluationRunner` now verifies the exact scoped diagnosis/hypothesis
  artifacts, creates a fresh baseline/candidate resource per rollout, balances execution
  position, retains one artifact per pair, enforces the frozen budget, and emits paired
  95% confidence evidence; subject failure becomes negative evidence while evaluator
  failure invalidates the experiment;
- `AgnoEvaluationSubject` now supplies the narrow model-execution bridge: a host factory
  creates a fresh Agno Agent per rollout, opaque sessions prevent raw case identity
  leakage, and only JSON-like content plus bounded public token/cost metrics enter the
  independent verifier; it has no judging, ledger, edit, or promotion authority;
- `EvaluationCorpusManifest` now freezes content-free ordered membership, source usage
  and retention provenance, semantic lineages, split exposure, selection/sampling/access
  controls, and independent curator/decontamination evidence; exact duplication,
  cross-split lineage, proposer-curator conflict, and known/unresolved overlap fail
  before model construction, and the default gate rejects ungoverned evidence;
- `pareto_frontier()` retains non-dominated quality/safety/cost/latency/complexity
  trade-offs without inventing a scalar fitness function, while the durable candidate
  ledger retains rejected and inconclusive experiments;
- that ledger now exposes an exact-owner, content-free, descending keyset query for
  rejected and inconclusive evaluations by stable gate reason, evaluator, mechanism,
  target, and safety result; private notes, raw metrics, content, and artifact IDs never
  enter the projection or a model-facing tool.
- registered capability effects now persist an exact approval request before
  materialization, accept only an authority-matched callback or host decision, and
  reauthorize the resulting capability/argument/policy/authority/expiry-bound grant at
  the final no-effect boundary; the model and improvement proposer cannot call the
  approval administration API or edit its permission control plane.

Custom-backend exact-state observers and production reconciliation partition/failover/
soak remain open. Runner-schema-1.2 local process subjects now bind and isolate rollout
state/faults; a strict immutable/no-network Docker profile adds non-root read-only
execution, seccomp, zero capabilities, resource bounds, and exact-owner cleanup. VM and
provider credential/egress isolation, enforced
sealed-corpus ACL/registry and semantic near-duplicate certification, richer statistical
policies, reversible non-knowledge
stores, and broad/previous-version model-backed benefit remain release blockers. One
frozen local Agno Learned Knowledge versus no-learning smoke now proves the exact
mechanism/model/corpus configuration only. The
content-free governed-corpus foundation, default exact Agno Learned Knowledge observer, and dedicated
leased/fenced/checkpointed SQLite/PostgreSQL worker are implemented, as is the fresh Agno
model-subject adapter; the separate paired outcome gate—not their mere presence—is the
benefit evidence. The
presence of a LearningMachine—or merely a populated candidate ledger—is never presented
as proof of self-improvement. See the implemented
[evaluation contract](self-improvement-evaluation.md) for its exact guarantees and
non-claims.

## Idea-to-plan traceability

| Research finding | Existing 0.12 coverage | Reconciliation added to the plan |
|---|---|---|
| A harness should be deliberately simple and generic, like an OS around the model. | R1-R3 and the three-profile public grammar. | Retain seven observable component classes without exposing seven new peer APIs. The manifest is internal/operator-facing. |
| Long work should keep rich state, logs, and artifacts in files instead of filling model context. | R5/R7 ArtifactStore, searchable trajectory, compaction, and rehydration. | Require full evidence history to remain read-only/searchable while only layered summaries and selected artifacts enter context. |
| Parallel subagents and background jobs need launch, inspect, cancel, logs, and durable handoff. | R11 child runs/jobs, budgets, joins, artifacts, and fencing. | Make child output/log/status artifacts inspectable and mergeable without copying entire child context into the parent. |
| ACE avoids context collapse by incrementally merging identified entries instead of repeatedly rewriting one prompt blob. | R5 bounded context and R8 typed candidates. | Add an itemized context logbook with stable IDs, deterministic merge, provenance, periodic refine/dedupe, and no destructive full-blob rewrite. |
| Generator, Reflector, and Curator have different responsibilities; MCE separates the context mechanism from its content. | Observe/candidate/validate/promote pipeline and Agno store-by-intent design. | Record role, input evidence, mechanism version, and produced artifact separately; do not let reflection directly become trusted context or code. |
| Self-Harness mines causal weakness patterns, proposes bounded changes, and accepts only held-in/held-out non-regressions. | Candidate evidence, shadow/held-out rollouts, rollback. | Add causal failure clusters, bounded editable surfaces, passing behaviors to preserve, prior-attempt history, and explicit held-in plus held-out acceptance. |
| AHE requires component, experience, and decision observability. | Versioned capabilities, trajectory, learning provenance, telemetry. | Add a seven-class `HarnessComponentManifest`; layered trace → per-run diagnosis → benchmark overview; and a falsifiable `ChangeHypothesis` for every improvement. |
| The evolver must not edit its verifier, runs, model/config, budget, or permission layer. | T10 policy boundary and experimental mutation disabled by default. | Make evaluation control-plane artifacts immutable/read-only to the proposer and default all component edit authority off. Attribute gains only when model/config/budget/data are pinned. |
| Scalar rewards and LLM judges are hackable; position and self-preference matter. Naive self-correction can regress without external feedback. | Deterministic-first evaluation, safety tests, human approval. | Separate proposer and evaluator contexts/authority, randomize/balance judge ordering, require deterministic evidence where possible, audit judge disagreements, and forbid self-reflection-only promotion. |
| Reward tampering includes influencing oversight or the reward/approval channel, not only exploiting a numeric metric. | Permissions are outside the improvement loop and promotion requires an explicit gate. | Durable approval-before-effect binds one exact action and authority; final-boundary reauthorization rejects drift, raw responses cannot approve, and approval administration is never model-callable or proposer-editable. |
| Evolution benefits from diversity, negative results, frozen transfer, and Pareto selection; fuzzy evaluation is a hard limit. | Candidate archive, task-class benefit, performance/cost/safety metrics. | Preserve rejected/negative candidates, apply novelty/diversity checks, maintain a Pareto frontier across quality/safety/cost/latency/complexity, and require transfer tests. No autonomous optimization where feedback is too weak to falsify the hypothesis. |

## Normative self-improvement record

Every proposed harness or durable learned-behavior change must have an immutable record
equivalent to:

```json
{
  "change_id": "hc_...",
  "target_component_ids": ["reviewer-skill"],
  "component_manifest_digests": ["sha256:..."],
  "failure_cluster_ids": ["fc_..."],
  "failure_cluster_digests": ["sha256:..."],
  "evidence_artifact_ids": ["artifact_..."],
  "inferred_root_cause": "...",
  "bounded_edit_surface": ["skills/reviewer/SKILL.md"],
  "predicted_fixes": ["..."],
  "at_risk_regressions": ["..."],
  "behaviors_to_preserve": ["..."],
  "model_config_digest": "sha256:...",
  "evaluator_digest": "sha256:...",
  "permission_digest": "sha256:...",
  "proposer_identity_digest": "sha256:...",
  "budget": {
    "max_rollouts": 20,
    "max_tokens": 200000,
    "max_wall_seconds": 3600,
    "max_cost_usd": 20
  },
  "rollback_target": "git:baseline",
  "proposed_by": "generator"
}
```

The seven component classes are system prompt, tool description, tool implementation,
middleware, skill, subagent configuration, and long-term memory. A concrete change may
touch more than one, but the proposal must say why. Runtime policy, identity, secrets,
verifiers, benchmark cases, raw traces, accepted budgets, and model configuration are
control-plane inputs, not editable components.

## Acceptance contract

An experimental self-improvement candidate may be accepted only when all of these are
true:

1. Verifier-grounded failures are grouped by causal mechanism, not merely terminal
   label such as timeout or missing artifact.
2. The proposal is bounded and names evidence, root cause, predicted benefit,
   regressions at risk, passing behavior to retain, and edit authority.
3. Held-in cases show the targeted weakness is improved; untouched held-out cases show
   no unacceptable regression; safety and privacy gates remain absolute.
4. Quality is evaluated with cost, latency, safety, and added complexity. Qualified
   candidates join a Pareto archive rather than being collapsed into one gameable score.
5. A frozen candidate transfers to at least one disjoint task class or environment
   before any broad “improved harness” claim.
6. Negative and rejected results remain searchable through an owner-scoped,
   content-free read model. Diversity/novelty controls prevent an archive of superficial
   variants.
7. The evaluator, benchmark, run records, model/config digest, permissions, and budgets
   are immutable to the proposer. LLM judges use balanced ordering and disagreement
   audit; objective verifiers lead whenever available. Approval administration remains
   a trusted host boundary and grants only the exact reviewed action.
8. Promotion is explicit, versioned, reversible, and disabled by default in the stable
   0.12 profile. A user or operator can inspect, reject, roll back, and delete it.

If a task has slow, ambiguous, or heuristic-only feedback, agnoclaw may collect and
explain candidates but must not claim autonomous self-improvement. Human judgment stays
at the appropriate decision boundary.

## What agnoclaw deliberately does not copy

- It does not expose autonomous source-code or policy mutation as a stable feature.
- It does not put all trajectories or learned items into every prompt.
- It does not trust chain-of-thought or self-critique as outcome evidence.
- It does not optimize one benchmark score while hiding cost, latency, safety, or
  maintainability regressions.
- It does not build a second model-training system; 0.12 improves the runtime and
  governed non-parametric context around Agno models.

## Release mapping

- T7 owns the itemized context logbook and lossless evidence access.
- T8 owns candidates, component/change manifests, causal weakness mining, promotion,
  rollback, negative-result preservation, and Agno LearningGateway integration.
- T10a/T10b own immutable evaluator/permission boundaries and edit authority.
- T12 owns held-in/held-out/transfer evaluation, judge calibration, Pareto governance,
  diversity, and claim evidence. Its immutable typed gate, scoped fresh-resource paired
  runner, artifact verification, 95% confidence records, public Agno Agent subject
  adapter, fail-closed governed-corpus manifest/evidence boundary, and negative-result
  ledger query are implemented. Its schema-v5 content-free typed projection and bounded
  PostgreSQL 17 noisy-neighbor gate now cover the measured 10,000-evaluation owner path;
  runner-schema-1.2 local process/state isolation and the strict Docker profile are also implemented. Managed/enforced
  corpus operations, richer statistics, production-scale archive/failover certification,
  multi-provider model-backed benefit, VM isolation, and provider credential/egress
  certification remain.
- T13 owns component/experience/decision observability and layered inspection.
- T15 keeps this reconciliation and its executable documentation current.
