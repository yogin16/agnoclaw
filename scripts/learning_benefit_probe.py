#!/usr/bin/env python3
"""Measure Agno Learned Knowledge against an identical no-learning control.

This is an opt-in live-model certification probe. It uses synthetic facts only and
prints a content-free summary; detailed paired evidence is retained only when the
operator supplies an empty ``--evidence-dir``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import ipaddress
import json
import re
import sys
import tempfile
from contextlib import AbstractContextManager, nullcontext, redirect_stdout
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from agnoclaw import (
    ArtifactScope,
    ChangeBudget,
    ChangeHypothesis,
    EvaluationCase,
    EvaluationCaseExposure,
    EvaluationCorpusEntry,
    EvaluationCorpusManifest,
    EvaluationGatePolicy,
    EvaluationRollout,
    EvaluationScore,
    EvaluationSlice,
    FailureCluster,
    HarnessComponentClass,
    HarnessComponentManifest,
    ImprovementEvaluationGate,
    ImprovementEvaluationRunner,
    ImprovementRole,
    LocalArtifactStore,
    agno_evaluation_subject_factory,
    evaluation_corpus_case_set_digest,
)
from agnoclaw.models.ownership import OwnedAgnoModelResource

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_TOKENS = (
    "KESTREL-731",
    "QUARTZ-284",
    "MISTRAL-906",
    "HARBOR-417",
    "SABLE-552",
    "TIDELINE-663",
)
_FACTS = (
    (
        "held-in-violet-relay",
        EvaluationSlice.HELD_IN,
        "violet relay",
        "violet relay",
        _TOKENS[0],
    ),
    (
        "held-in-amber-router",
        EvaluationSlice.HELD_IN,
        "amber router",
        "amber router",
        _TOKENS[1],
    ),
    (
        "held-out-cobalt-beacon",
        EvaluationSlice.HELD_OUT,
        "cobalt beacon",
        "cobalt beacon",
        _TOKENS[2],
    ),
    (
        "held-out-silver-conduit",
        EvaluationSlice.HELD_OUT,
        "silver conduit",
        "silver conduit",
        _TOKENS[3],
    ),
    (
        "transfer-obsidian-lattice",
        EvaluationSlice.TRANSFER,
        "obsidian lattice, also called the black-glass lattice",
        "black-glass lattice",
        _TOKENS[4],
    ),
    (
        "transfer-coral-bridge",
        EvaluationSlice.TRANSFER,
        "coral bridge, also called the reef-colored bridge",
        "reef-colored bridge",
        _TOKENS[5],
    ),
)


class ProbeConfigurationError(RuntimeError):
    """An unsafe or incomplete probe configuration."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run a paired live-model proof of Agno Learned Knowledge benefit against "
            "an identical no-learning control."
        )
    )
    parser.add_argument("--model", default="qwen3:0.6b")
    parser.add_argument("--embedder", default="nomic-embed-text")
    parser.add_argument("--embedder-dimensions", type=int, default=768)
    parser.add_argument("--ollama-host", default="http://127.0.0.1:11434")
    parser.add_argument("--per-rollout-timeout", type=float, default=90.0)
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument(
        "--allow-live-model",
        action="store_true",
        help="Required acknowledgement that the probe invokes a live Ollama model.",
    )
    parser.add_argument(
        "--allow-remote-ollama",
        action="store_true",
        help="Required in addition to --allow-live-model for a non-loopback host.",
    )
    return parser


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _is_loopback_host(host: str) -> bool:
    parsed = urlsplit(host)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ProbeConfigurationError(
            "--ollama-host must be an http(s) origin without credentials, path, query, or fragment"
        )
    if parsed.hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        return False


def _model_inventory(host: str) -> dict[str, str]:
    request = Request(f"{host.rstrip('/')}/api/tags", headers={"Accept": "application/json"})
    with urlopen(request, timeout=10) as response:  # noqa: S310 - validated operator origin
        payload = json.load(response)
    values: dict[str, str] = {}
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        raise ProbeConfigurationError("Ollama returned an invalid model inventory")
    for item in models:
        if not isinstance(item, dict):
            continue
        digest = item.get("digest")
        if not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None:
            continue
        for field_name in ("name", "model"):
            model_name = item.get(field_name)
            if isinstance(model_name, str) and model_name:
                values[model_name] = f"sha256:{digest}"
    return values


def _resolve_model(inventory: dict[str, str], requested: str) -> tuple[str, str]:
    candidates = (requested, f"{requested}:latest") if ":" not in requested else (requested,)
    for candidate in candidates:
        digest = inventory.get(candidate)
        if digest is not None:
            return candidate, digest
    raise ProbeConfigurationError(f"Ollama is missing required model: {requested}")


def _cases() -> tuple[EvaluationCase, ...]:
    return tuple(
        EvaluationCase(
            case_id=case_id,
            slice=slice_name,
            task_class="synthetic-protocol-recall",
            payload={
                "prompt": (
                    "This is a synthetic protocol lookup. What is the exact protocol "
                    f"token for the {query_name}? Reply with only the token. If the "
                    "token is unavailable, reply UNKNOWN."
                ),
                "expected_token": token,
            },
        )
        for case_id, slice_name, _knowledge_name, query_name, token in _FACTS
    )


def _input_builder(case: EvaluationCase) -> str:
    payload = case.to_dict()["payload"]
    return str(payload["prompt"])


def _verifier(case: EvaluationCase, rollout: EvaluationRollout) -> EvaluationScore:
    payload = case.to_dict()["payload"]
    expected = str(payload["expected_token"])
    output = str(rollout.output).upper()
    other_token_present = any(token != expected and token in output for token in _TOKENS)
    bounded = len(output.encode("utf-8")) <= 2_048
    correct = expected in output and not other_token_present
    safe = bounded and not other_token_present
    return EvaluationScore(
        quality=1.0 if correct else 0.0,
        safety=1.0 if safe else 0.0,
        safety_passed=safe,
        privacy_passed=not other_token_present,
        objective=True,
    )


def _evidence_context(
    requested: Path | None,
) -> tuple[AbstractContextManager[str | Path], bool]:
    if requested is None:
        return tempfile.TemporaryDirectory(prefix="agnoclaw-learning-benefit-"), False
    resolved = requested.expanduser().resolve()
    if resolved.exists() and any(resolved.iterdir()):
        raise ProbeConfigurationError("--evidence-dir must not already contain files")
    resolved.mkdir(parents=True, exist_ok=True)
    return nullcontext(resolved), True


async def _stage_experiment_inputs(
    store: LocalArtifactStore,
    scope: ArtifactScope,
    *,
    cases: tuple[EvaluationCase, ...],
    model_config_digest: str,
    component_digest: str,
) -> tuple[
    HarnessComponentManifest,
    FailureCluster,
    ChangeHypothesis,
    EvaluationCorpusManifest,
    tuple[Any, ...],
]:
    hypothesis_evidence = await store.stage_json(
        {
            "type": "agnoclaw.learning_benefit_hypothesis_evidence",
            "schema_version": "1.0",
            "claim": "read-only learned-knowledge context improves objective recall",
            "case_count": len(cases),
        },
        scope=scope,
        purpose="improvement_hypothesis_evidence",
    )
    cluster_evidence = await store.stage_json(
        {
            "type": "agnoclaw.learning_benefit_failure_cluster_evidence",
            "schema_version": "1.0",
            "failure_class": "knowledge_unavailable_without_learning",
            "content_retained": False,
        },
        scope=scope,
        purpose="improvement_cluster_evidence",
    )
    manifest = HarnessComponentManifest(
        component_id="agno-learned-knowledge-context",
        component_class=HarnessComponentClass.LONG_TERM_MEMORY,
        version=f"agno-{version('agno')}",
        implementation_digest=component_digest,
        editable_paths=("src/agnoclaw/learning.py",),
        rollback_reference="control:no-learning",
        description="Read-only injection of promoted Agno Learned Knowledge.",
    )
    evaluator_digest = _canonical_digest(
        {
            "verifier": "exact-expected-token-with-no-cross-token-v1",
            "maximum_output_bytes": 2_048,
            "objective": True,
        }
    )
    cluster = FailureCluster(
        cluster_id="no-learning-context-recall-failure",
        causal_mechanism="institutional knowledge is unavailable to a no-learning subject",
        verifier_digest=evaluator_digest,
        failure_run_ids=("synthetic-no-learning-control",),
        evidence_artifact_ids=(cluster_evidence.artifact_id,),
        terminal_labels=("incorrect_protocol_token",),
        mechanism_version="objective-token-cluster:v1",
    )
    proposer_digest = _canonical_digest({"role": "learning-benefit-proposer-v1"})
    hypothesis = ChangeHypothesis(
        change_id="enable-read-only-learned-knowledge-context",
        target_component_ids=(manifest.component_id,),
        component_manifest_digests=(manifest.digest,),
        failure_cluster_ids=(cluster.cluster_id,),
        failure_cluster_digests=(cluster.digest,),
        evidence_artifact_ids=(hypothesis_evidence.artifact_id,),
        inferred_root_cause=(
            "The control cannot access authorized institutional facts that are present "
            "in Agno Learned Knowledge."
        ),
        bounded_edit_surface=manifest.editable_paths,
        predicted_fixes=("retrieve the relevant learned fact into model context",),
        at_risk_regressions=("latency increase", "irrelevant-learning disclosure"),
        behaviors_to_preserve=(
            "identical model prompt and instructions",
            "no model-facing learning write tools",
        ),
        previous_attempt_ids=(),
        model_config_digest=model_config_digest,
        evaluator_digest=evaluator_digest,
        permission_digest=_canonical_digest(
            {
                "learning_access": "read-only-context",
                "agent_tools": False,
                "synthetic_data_only": True,
            }
        ),
        proposer_identity_digest=proposer_digest,
        budget=ChangeBudget(
            max_rollouts=len(cases) * 2,
            max_tokens=250_000,
            max_wall_seconds=1_200,
            max_cost_usd=1.0,
        ),
        rollback_target="control:no-learning",
        proposed_by=ImprovementRole.GENERATOR,
        mechanism_version="learning-benefit-hypothesis:v1",
    )

    source = await store.stage_json(
        {
            "type": "agnoclaw.evaluation_corpus_source",
            "schema_version": "1.0",
            "source_id": "agnoclaw-synthetic-protocol-recall-v1",
            "source_digest": _canonical_digest(
                {"case_ids": [case.case_id for case in cases], "synthetic": True}
            ),
            "usage_basis": "internal_authorized",
            "retention_policy_digest": _canonical_digest(
                {"class": "synthetic-evaluation", "operator_controlled": True}
            ),
        },
        scope=scope,
        purpose="evaluation_corpus_source",
    )
    entries = tuple(
        EvaluationCorpusEntry.from_case(
            case,
            lineage_digest=_canonical_digest({"lineage": case.case_id}),
            source_artifact_id=source.artifact_id,
            exposure=(
                EvaluationCaseExposure.DEVELOPMENT
                if case.slice is EvaluationSlice.HELD_IN
                else EvaluationCaseExposure.SEALED
            ),
        )
        for case in cases
    )
    method_digest = _canonical_digest(
        {"method": "exact-token-and-semantic-lineage-review", "version": 1}
    )
    curator_digest = _canonical_digest({"role": "independent-corpus-curator-v1"})
    decontamination = await store.stage_json(
        {
            "type": "agnoclaw.evaluation_corpus_decontamination",
            "schema_version": "1.0",
            "case_set_digest": evaluation_corpus_case_set_digest(entries),
            "method_digest": method_digest,
            "checked_case_count": len(entries),
            "reviewer_identity_digest": curator_digest,
            "comparison_corpus_digests": (
                [_canonical_digest({"comparison": "no-learning-control"})]
            ),
            "known_overlap_case_ids": [],
            "unresolved_case_ids": [],
        },
        scope=scope,
        purpose="evaluation_corpus_decontamination",
    )
    corpus = EvaluationCorpusManifest(
        corpus_id="agnoclaw-synthetic-protocol-recall",
        version="1",
        entries=entries,
        selection_policy_digest=_canonical_digest(
            {"policy": "two-cases-per-required-slice"}
        ),
        sampling_seed_digest=_canonical_digest({"seed": "fixed-probe-v1"}),
        sealed_access_policy_digest=_canonical_digest(
            {"held_out_and_transfer": "sealed-from-proposer"}
        ),
        decontamination_method_digest=method_digest,
        decontamination_artifact_id=decontamination.artifact_id,
        curator_identity_digest=curator_digest,
    )
    return (
        manifest,
        cluster,
        hypothesis,
        corpus,
        (hypothesis_evidence, cluster_evidence, source, decontamination),
    )


async def _run_probe(args: argparse.Namespace) -> dict[str, Any]:
    try:
        import ollama
        from agno.agent import Agent
        from agno.db.sqlite import SqliteDb
        from agno.knowledge.embedder.ollama import OllamaEmbedder
        from agno.knowledge.knowledge import Knowledge
        from agno.learn import LearnedKnowledgeConfig, LearningMachine, LearningMode
        from agno.models.ollama import Ollama
        from agno.vectordb.lancedb import LanceDb
    except ImportError as exc:
        raise ProbeConfigurationError(
            "live learning proof requires `uv run --isolated --extra local --extra rag ...`"
        ) from exc

    inventory = await asyncio.to_thread(_model_inventory, args.ollama_host)
    resolved_model, model_digest = _resolve_model(inventory, args.model)
    resolved_embedder, embedder_digest = _resolve_model(inventory, args.embedder)
    agno_version = version("agno")
    model_config_digest = _canonical_digest(
        {
            "provider": "ollama",
            "host_class": "loopback" if _is_loopback_host(args.ollama_host) else "remote",
            "model": resolved_model,
            "model_digest": model_digest,
            "embedder": resolved_embedder,
            "embedder_digest": embedder_digest,
            "embedder_dimensions": args.embedder_dimensions,
            "agno_version": agno_version,
            "decoding": {
                "temperature": 0,
                "seed": 20_260_816,
                "think": True,
            },
        }
    )
    component_digest = _canonical_digest(
        {
            "component": "agno-learned-knowledge-context",
            "agno_version": agno_version,
            "learning_mode": "agentic",
            "agent_tools": False,
            "model_config_digest": model_config_digest,
        }
    )
    evidence_context, retained = _evidence_context(args.evidence_dir)
    with evidence_context as evidence_value:
        evidence_root = Path(evidence_value)
        store = LocalArtifactStore(evidence_root / "artifacts")
        run_id = f"learning-benefit-{uuid4().hex}"
        scope = ArtifactScope(
            run_id=run_id,
            tenant_id="synthetic-evaluation",
            user_id="learning-benefit-probe",
        )
        cases = _cases()
        manifest, cluster, hypothesis, corpus, upstream = await _stage_experiment_inputs(
            store,
            scope,
            cases=cases,
            model_config_digest=model_config_digest,
            component_digest=component_digest,
        )

        database = SqliteDb(db_file=str(evidence_root / "agno-learning.db"))
        sync_embed_client = ollama.Client(host=args.ollama_host)
        async_embed_client = ollama.AsyncClient(host=args.ollama_host)
        embedder_resource = OwnedAgnoModelResource(
            SimpleNamespace(client=sync_embed_client, async_client=async_embed_client)
        )
        embedder = OllamaEmbedder(
            id=resolved_embedder,
            dimensions=args.embedder_dimensions,
            ollama_client=sync_embed_client,
            async_client=async_embed_client,
        )
        vector_db = LanceDb(
            uri=evidence_root / "lancedb",
            table_name="learned_knowledge",
            embedder=embedder,
        )
        knowledge = Knowledge(vector_db=vector_db)
        seed_machine = LearningMachine(
            db=database,
            knowledge=knowledge,
            learned_knowledge=LearnedKnowledgeConfig(
                knowledge=knowledge,
                mode=LearningMode.AGENTIC,
                namespace="learning-benefit-probe",
                enable_agent_tools=False,
                agent_can_save=False,
                agent_can_search=False,
            ),
            namespace="learning-benefit-probe",
        )
        learning_store: Any = seed_machine.learned_knowledge_store
        if learning_store is None:
            raise RuntimeError("Agno did not initialize the Learned Knowledge store")
        try:
            for case_id, _slice_name, knowledge_name, _query_name, token in _FACTS:
                title = f"Synthetic protocol token for {knowledge_name}"
                saved = await asyncio.to_thread(
                    learning_store.save,
                    title,
                    f"The {knowledge_name} uses the exact protocol token {token}.",
                    "Authorized synthetic fact for the Agnoclaw learning-benefit probe.",
                    ["agnoclaw", "synthetic", case_id],
                    None,
                    None,
                    None,
                    "learning-benefit-probe",
                )
                if not saved:
                    raise RuntimeError(f"Agno did not save the synthetic fact for {case_id}")

            instructions = (
                "Answer the synthetic protocol lookup using available context. Before "
                "replying UNKNOWN, inspect any <relevant_learnings> block for a matching "
                "protocol name or alias. If a matching learning exists, copy its exact "
                "token. Return only that token or UNKNOWN. Never invent a token and never "
                "return unrelated protocol tokens."
            )

            def evaluation_model() -> Ollama:
                return Ollama(
                    id=resolved_model,
                    host=args.ollama_host,
                    options={"temperature": 0, "seed": 20_260_816},
                    request_params={"think": True},
                )

            def baseline_agent() -> Agent:
                return Agent(
                    model=evaluation_model(),
                    id="agnoclaw-learning-benefit-control",
                    db=database,
                    learning=None,
                    add_learnings_to_context=True,
                    instructions=instructions,
                    markdown=False,
                    telemetry=False,
                )

            def candidate_agent() -> Agent:
                learning = LearningMachine(
                    db=database,
                    knowledge=knowledge,
                    learned_knowledge=LearnedKnowledgeConfig(
                        knowledge=knowledge,
                        mode=LearningMode.AGENTIC,
                        namespace="learning-benefit-probe",
                        enable_agent_tools=False,
                        agent_can_save=False,
                        agent_can_search=False,
                    ),
                    namespace="learning-benefit-probe",
                )
                return Agent(
                    model=evaluation_model(),
                    id="agnoclaw-learning-benefit-candidate",
                    db=database,
                    learning=learning,
                    add_learnings_to_context=True,
                    instructions=instructions,
                    markdown=False,
                    telemetry=False,
                )

            runner = ImprovementEvaluationRunner(
                store,
                artifact_scope=scope,
                evaluator_identity_digest=_canonical_digest(
                    {"role": "independent-objective-evaluator-v1"}
                ),
                per_rollout_timeout=args.per_rollout_timeout,
            )
            result = await runner.run(
                candidate_id="agno-learned-knowledge-read-context",
                hypothesis=hypothesis,
                manifests=(manifest,),
                failure_clusters=(cluster,),
                cases=cases,
                baseline_factory=agno_evaluation_subject_factory(
                    baseline_agent,
                    input_builder=_input_builder,
                    session_prefix="agnoclaw-learning-control",
                    close_agent=True,
                ),
                candidate_factory=agno_evaluation_subject_factory(
                    candidate_agent,
                    input_builder=_input_builder,
                    session_prefix="agnoclaw-learning-candidate",
                    close_agent=True,
                ),
                baseline_digest=_canonical_digest(
                    {"model_config": model_config_digest, "learning": None}
                ),
                candidate_digest=_canonical_digest(
                    {
                        "model_config": model_config_digest,
                        "learning": "read-only-context",
                        "knowledge_set": _canonical_digest(
                            [
                                {"knowledge_name": item[2], "token": item[4]}
                                for item in _FACTS
                            ]
                        ),
                    }
                ),
                verifier=_verifier,
                upstream_artifacts=upstream,
                novelty_score=0.5,
                diversity_score=1.0,
                added_complexity=0.05,
                corpus_manifest=corpus,
            )
            gate = ImprovementEvaluationGate(
                EvaluationGatePolicy(
                    min_held_in_samples=2,
                    min_held_out_samples=2,
                    min_transfer_samples=2,
                    min_held_in_quality_delta=0.5,
                    max_cost_ratio=1_000,
                    max_latency_ratio=1_000,
                )
            )
            decision = gate.evaluate(
                result.report,
                hypothesis=hypothesis,
                manifests=(manifest,),
                failure_clusters=(cluster,),
            )
            statistics = {
                item.slice.value: item.to_dict() for item in result.report.paired_statistics
            }
            cross_slice_benefit = all(
                item.mean_delta > 0 and item.wins > 0 and item.losses == 0
                for item in result.report.paired_statistics
            )
            passed = decision.qualified and cross_slice_benefit
            reasons = list(decision.reasons)
            if decision.qualified and not cross_slice_benefit:
                reasons.append("positive_cross_slice_benefit_required")
            summary = {
                "type": "agnoclaw.learning_benefit_probe",
                "schema_version": "1.0",
                "status": "passed" if passed else "failed",
                "claim_scope": "one exact Ollama model/embedder/corpus configuration",
                "agno_version": agno_version,
                "model": resolved_model,
                "model_digest": model_digest,
                "embedder": resolved_embedder,
                "embedder_digest": embedder_digest,
                "model_config_digest": model_config_digest,
                "case_count": len(cases),
                "rollout_count": result.report.usage.rollouts,
                "execution_order_balanced": result.execution_order_balanced,
                "paired_statistics": statistics,
                "usage": result.report.usage.to_dict(),
                "gate_qualified": decision.qualified,
                "cross_slice_benefit": cross_slice_benefit,
                "verdict": decision.verdict.value,
                "reasons": reasons,
                "evaluation_digest": decision.evaluation_digest,
                "runner_digest": result.report.runner_digest,
                "corpus_manifest_digest": corpus.digest,
                "evidence_retained": retained,
                "evidence_artifact_count": len(result.evidence_artifacts),
            }
            summary_artifact = await store.stage_json(
                summary,
                scope=scope,
                purpose="learning_benefit_probe_summary",
            )
            summary["summary_artifact_checksum"] = summary_artifact.checksum
            if retained:
                summary["evidence_dir"] = str(evidence_root)
                summary["summary_artifact_id"] = summary_artifact.artifact_id
            return summary
        finally:
            database.close()
            await embedder_resource.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.allow_live_model:
        parser.error("--allow-live-model is required")
    if args.embedder_dimensions <= 0:
        parser.error("--embedder-dimensions must be positive")
    if args.per_rollout_timeout <= 0:
        parser.error("--per-rollout-timeout must be positive")
    try:
        loopback = _is_loopback_host(args.ollama_host)
        if not loopback and not args.allow_remote_ollama:
            parser.error("--allow-remote-ollama is required for a non-loopback Ollama host")
        # Agno's console logger currently writes informational messages to stdout.
        # Keep the probe's stdout a strict one-record JSON protocol for automation.
        with redirect_stdout(sys.stderr):
            summary = asyncio.run(_run_probe(args))
    except (ProbeConfigurationError, OSError, ValueError) as exc:
        print(f"learning benefit probe configuration failed: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"learning benefit probe failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
