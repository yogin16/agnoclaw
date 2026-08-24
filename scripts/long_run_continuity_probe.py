#!/usr/bin/env python3
"""Exercise bounded session continuity across 100+ turns and real reopen cycles.

The probe uses a deterministic host-supplied Agno model, a disposable SQLite Agno
session database, and agnoclaw's public context APIs.  It performs no network calls and
prints one content-free JSON record.  Detailed synthetic trajectory bytes are retained
only when the operator supplies an empty ``--evidence-dir``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import ipaddress
import json
import math
import re
import sys
import tempfile
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import AbstractContextManager, nullcontext, redirect_stdout
from importlib.metadata import version
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from agno.db.sqlite import SqliteDb
from agno.models.base import Model
from agno.models.response import ModelResponse

from agnoclaw import AgentHarness, ContextItemKind, LocalFileContextLockProvider
from agnoclaw.config import HarnessConfig, StorageConfig
from agnoclaw.models.ownership import OwnedAgnoModelResource
from agnoclaw.runtime import LocalArtifactStore

_PROTOCOL_VERSION = "1.1"
_DEFAULT_RESTART_TURNS = (40, 80)
_MARKER_NAMES = ("head", "middle", "tail")
_TURN_RE = re.compile(r"Synthetic continuity turn (?P<turn>[0-9]{3})\.")


class ProbeConfigurationError(RuntimeError):
    """The requested probe would not certify the documented contract."""


class ProbeProvider(NamedTuple):
    """Resolved provider identity without retaining credentials or response content."""

    kind: str
    model_id: str
    model_digest: str
    host: str | None
    host_class: str
    configuration_digest: str


class ContinuityToolTracker:
    """Record exact Agno-native probe tool execution without external effects."""

    def __init__(self, markers_by_turn: dict[int, str]) -> None:
        self._markers_by_turn = dict(markers_by_turn)
        self.calls: list[int] = []

    def continuity_probe_fact(self, turn: int) -> str:
        """Return one deterministic continuity observation for a requested turn."""
        if turn not in self._markers_by_turn:
            raise ValueError("continuity probe requested an undeclared tool turn")
        if turn in self.calls:
            raise AssertionError("continuity probe tool execution was duplicated")
        self.calls.append(turn)
        marker = self._markers_by_turn[turn]
        return f"Tool continuity observation for turn {turn:03d}. Durable fact: {marker}."


class ContinuityModel(Model):
    """Deterministic, provider-neutral model used to isolate harness behavior."""

    def __init__(self, *args: Any, tool_turns: frozenset[int] = frozenset(), **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._tool_turns = tool_turns
        self.invocations = 0

    def invoke(self, *args: Any, **kwargs: Any) -> ModelResponse:
        del args
        self.invocations += 1
        messages = kwargs.get("messages") or []
        last_message = messages[-1] if messages else None
        if getattr(last_message, "role", None) == "tool":
            return ModelResponse(content="Synthetic tool-bearing step acknowledged.")
        content = getattr(last_message, "content", "")
        match = _TURN_RE.search(content) if isinstance(content, str) else None
        if match is not None:
            turn = int(match.group("turn"))
            if turn in self._tool_turns:
                return ModelResponse(
                    tool_calls=[
                        {
                            "id": f"continuity-tool-call-{turn:03d}",
                            "type": "function",
                            "function": {
                                "name": "continuity_probe_fact",
                                "arguments": json.dumps({"turn": turn}, separators=(",", ":")),
                            },
                        }
                    ]
                )
        return ModelResponse(content="Synthetic continuity step acknowledged.")

    async def ainvoke(self, *args: Any, **kwargs: Any) -> ModelResponse:
        return self.invoke(*args, **kwargs)

    def invoke_stream(self, *args: Any, **kwargs: Any) -> Iterator[ModelResponse]:
        yield self.invoke(*args, **kwargs)

    async def ainvoke_stream(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> AsyncIterator[ModelResponse]:
        yield self.invoke(*args, **kwargs)

    def count_tokens(
        self,
        messages: list[Any],
        tools: Sequence[Any] | None = None,
        output_schema: Any | None = None,
    ) -> int:
        del tools, output_schema
        size = 0
        for message in messages:
            role = getattr(message, "role", "")
            content = getattr(message, "content", "")
            if not isinstance(content, str):
                content = json.dumps(content, default=str, ensure_ascii=False, sort_keys=True)
            size += len(f"{role}:{content}".encode())
        return math.ceil(size / 4)

    def _parse_provider_response(self, response: Any, **kwargs: Any) -> ModelResponse:
        del kwargs
        return ModelResponse(content=str(response))

    def _parse_provider_response_delta(self, response: Any) -> ModelResponse:
        return ModelResponse(content=str(response))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run a deterministic 100+ turn agnoclaw context continuity, compaction, "
            "reopen, retrieval, and rehydration probe."
        )
    )
    parser.add_argument("--turns", type=int, default=120)
    parser.add_argument("--max-context-tokens", type=int, default=1_800)
    parser.add_argument(
        "--restart-turns",
        default=",".join(str(value) for value in _DEFAULT_RESTART_TURNS),
        help="Comma-separated completed-turn boundaries at which to close and reopen.",
    )
    parser.add_argument(
        "--tool-every",
        type=int,
        default=0,
        help=(
            "Exercise an Agno-native, side-effect-free tool every N turns plus the "
            "head/middle/tail marker turns; zero disables tools."
        ),
    )
    parser.add_argument(
        "--provider",
        choices=("deterministic", "ollama"),
        default="deterministic",
        help="Provider used for Agno model calls; Ollama is an explicit live gate.",
    )
    parser.add_argument("--model", default="qwen3:0.6b")
    parser.add_argument("--ollama-host", default="http://127.0.0.1:11434")
    parser.add_argument("--provider-timeout", type=float, default=120.0)
    parser.add_argument(
        "--allow-live-model",
        action="store_true",
        help="Required acknowledgement when --provider ollama invokes a live model.",
    )
    parser.add_argument(
        "--allow-remote-ollama",
        action="store_true",
        help="Also required when the Ollama origin is not loopback.",
    )
    parser.add_argument("--evidence-dir", type=Path)
    return parser


def _parse_restart_turns(value: str, *, turns: int) -> tuple[int, ...]:
    if not value.strip():
        return ()
    try:
        parsed = tuple(int(item.strip()) for item in value.split(","))
    except ValueError as exc:
        raise ProbeConfigurationError("--restart-turns must contain only integers") from exc
    if len(set(parsed)) != len(parsed) or tuple(sorted(parsed)) != parsed:
        raise ProbeConfigurationError("--restart-turns must be unique and increasing")
    if any(item <= 0 or item >= turns for item in parsed):
        raise ProbeConfigurationError(
            "--restart-turns must be greater than zero and smaller than --turns"
        )
    return parsed


def _validate_args(args: argparse.Namespace) -> tuple[int, ...]:
    if args.turns < 100:
        raise ProbeConfigurationError("--turns must be at least 100")
    if not 800 <= args.max_context_tokens <= 1_000_000:
        raise ProbeConfigurationError(
            "--max-context-tokens must be between 800 and 1000000"
        )
    if args.tool_every < 0 or args.tool_every > args.turns:
        raise ProbeConfigurationError("--tool-every must be zero or between 1 and --turns")
    if args.provider_timeout <= 0:
        raise ProbeConfigurationError("--provider-timeout must be positive")
    if args.provider == "ollama" and not args.allow_live_model:
        raise ProbeConfigurationError("--allow-live-model is required for --provider ollama")
    return _parse_restart_turns(args.restart_turns, turns=args.turns)


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
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
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


async def _resolve_provider(args: argparse.Namespace) -> ProbeProvider:
    agno_version = version("agno")
    if args.provider == "deterministic":
        identity: dict[str, Any] = {
            "provider": "deterministic",
            "model": "agnoclaw-continuity-model",
            "protocol_version": _PROTOCOL_VERSION,
            "agno_version": agno_version,
        }
        return ProbeProvider(
            kind="deterministic",
            model_id="agnoclaw-continuity-model",
            model_digest=_canonical_digest(identity),
            host=None,
            host_class="none",
            configuration_digest=_canonical_digest(identity),
        )

    loopback = _is_loopback_host(args.ollama_host)
    if not loopback and not args.allow_remote_ollama:
        raise ProbeConfigurationError(
            "--allow-remote-ollama is required for a non-loopback Ollama host"
        )
    inventory = await asyncio.to_thread(_model_inventory, args.ollama_host)
    model_id, model_digest = _resolve_model(inventory, args.model)
    identity = {
        "provider": "ollama",
        "host_class": "loopback" if loopback else "remote",
        "model": model_id,
        "model_digest": model_digest,
        "agno_version": agno_version,
        "decoding": {"temperature": 0, "seed": 20_260_817, "think": False},
    }
    return ProbeProvider(
        kind="ollama",
        model_id=model_id,
        model_digest=model_digest,
        host=args.ollama_host,
        host_class="loopback" if loopback else "remote",
        configuration_digest=_canonical_digest(identity),
    )


def _evidence_context(
    requested: Path | None,
) -> tuple[AbstractContextManager[str | Path], bool]:
    if requested is None:
        return tempfile.TemporaryDirectory(prefix="agnoclaw-long-run-"), False
    resolved = requested.expanduser().resolve()
    if resolved.exists() and any(resolved.iterdir()):
        raise ProbeConfigurationError("--evidence-dir must not already contain files")
    resolved.mkdir(parents=True, exist_ok=True)
    return nullcontext(resolved), True


def _marker(turn: int, *, label: str) -> str:
    digest = hashlib.sha256(f"{_PROTOCOL_VERSION}:{label}:{turn}".encode()).hexdigest()[:16]
    return f"continuity-{label}-{digest}"


def _tool_marker(turn: int, *, label: str) -> str:
    digest = hashlib.sha256(
        f"{_PROTOCOL_VERSION}:tool-result:{label}:{turn}".encode()
    ).hexdigest()[:16]
    return f"tool-continuity-{label}-{digest}"


def _marker_turns(turns: int) -> dict[str, int]:
    return {
        "head": 1,
        "middle": max(2, (turns + 1) // 2),
        "tail": turns,
    }


def _prompt(
    turn: int,
    *,
    markers: dict[str, tuple[int, str]],
    tool_required: bool = False,
) -> str:
    active = [marker for marker_turn, marker in markers.values() if marker_turn == turn]
    marker_clause = f" Durable fact: {active[0]}." if active else ""
    tool_clause = (
        f" You MUST call continuity_probe_fact exactly once with turn={turn}; do not "
        "invent its result, and after the tool returns acknowledge the step."
        if tool_required
        else " Do not call any tool on this turn."
    )
    padding = " ".join(f"bounded-evidence-{turn:03d}-{index:02d}" for index in range(16))
    return (
        f"Synthetic continuity turn {turn:03d}.{marker_clause} Preserve the current "
        f"plan, do not execute external effects, and acknowledge this step.{tool_clause} "
        f"{padding}"
    )


def _configuration(root: Path) -> HarnessConfig:
    return HarnessConfig(
        workspace_dir=str(root / "workspace"),
        storage=StorageConfig(sqlite_path=str(root / "sessions.db")),
    )


def _open_harness(
    root: Path,
    *,
    max_context_tokens: int,
    tool_turns: frozenset[int],
    tool_tracker: ContinuityToolTracker | None,
    provider: ProbeProvider,
    provider_timeout: float,
) -> tuple[AgentHarness, SqliteDb, LocalArtifactStore, Model, OwnedAgnoModelResource | None]:
    database = SqliteDb(db_file=str(root / "sessions.db"))
    artifact_store = LocalArtifactStore(root / "artifacts")
    model_resource: OwnedAgnoModelResource | None = None
    if provider.kind == "deterministic":
        model: Model = ContinuityModel(
            id=provider.model_id,
            tool_turns=tool_turns,
        )
    else:
        try:
            from agno.models.ollama import Ollama
        except ImportError as exc:
            raise ProbeConfigurationError(
                "live continuity requires `uv run --isolated --extra local ...`"
            ) from exc
        model = Ollama(
            id=provider.model_id,
            host=provider.host,
            timeout=provider_timeout,
            options={"temperature": 0, "seed": 20_260_817},
            request_params={"think": False},
        )
        model_resource = OwnedAgnoModelResource(model)
    harness = AgentHarness(
        model=model,
        config=_configuration(root),
        db=database,
        artifact_store=artifact_store,
        include_default_tools=False,
        tools=[tool_tracker.continuity_probe_fact] if tool_tracker is not None else None,
        instructions=(
            "This is a deterministic, synthetic continuity probe. Use only the "
            "declared side-effect-free probe tool when requested; perform no external effects."
        ),
        session_id="continuity-session",
        user_id="continuity-user",
        tenant_id="continuity-tenant",
        max_context_tokens=max_context_tokens,
        auto_compact_context=True,
        context_lock_provider=LocalFileContextLockProvider(root / "context-locks"),
    )
    return harness, database, artifact_store, model, model_resource


async def _close_harness(
    harness: AgentHarness,
    database: SqliteDb,
    model_resource: OwnedAgnoModelResource | None,
) -> None:
    try:
        await harness.aclose()
    finally:
        try:
            database.close()
        finally:
            if model_resource is not None:
                await model_resource.aclose()


def _run_metadata(value: Any) -> dict[str, Any]:
    metadata = getattr(value, "metadata", None)
    return dict(metadata) if isinstance(metadata, dict) else {}


async def _exercise(
    root: Path,
    *,
    turns: int,
    max_context_tokens: int,
    restart_turns: tuple[int, ...],
    tool_every: int,
    evidence_retained: bool,
    provider: ProbeProvider,
    provider_timeout: float,
) -> dict[str, Any]:
    marker_turns = _marker_turns(turns)
    markers = {
        label: (turn, _marker(turn, label=label)) for label, turn in marker_turns.items()
    }
    tool_markers = {
        label: (turn, _tool_marker(turn, label=label))
        for label, turn in marker_turns.items()
    }
    tool_turns = (
        frozenset(range(tool_every, turns + 1, tool_every)).union(marker_turns.values())
        if tool_every
        else frozenset()
    )
    tool_markers_by_turn = {
        turn: next(
            (marker for marker_turn, marker in tool_markers.values() if marker_turn == turn),
            f"tool-continuity-turn-{turn:03d}",
        )
        for turn in tool_turns
    }
    tool_tracker = ContinuityToolTracker(tool_markers_by_turn) if tool_turns else None
    harness, database, artifact_store, model, model_resource = _open_harness(
        root,
        max_context_tokens=max_context_tokens,
        tool_turns=tool_turns,
        tool_tracker=tool_tracker,
        provider=provider,
        provider_timeout=provider_timeout,
    )
    deterministic_model_calls = 0
    observed_revision = 0
    reopen_count = 0
    try:
        for turn in range(1, turns + 1):
            tool_calls_before = len(tool_tracker.calls) if tool_tracker is not None else 0
            result = await harness.arun(
                _prompt(turn, markers=markers, tool_required=turn in tool_turns),
                session_id="continuity-session",
                user_id="continuity-user",
            )
            content = getattr(result, "content", None)
            if provider.kind == "deterministic":
                expected_content = (
                    "Synthetic tool-bearing step acknowledged."
                    if turn in tool_turns
                    else "Synthetic continuity step acknowledged."
                )
                if content != expected_content:
                    raise AssertionError(f"turn {turn} returned an unexpected model response")
            elif not isinstance(content, str) or not content.strip():
                raise AssertionError(f"live provider turn {turn} returned no text")

            tool_calls_after = len(tool_tracker.calls) if tool_tracker is not None else 0
            expected_delta = 1 if turn in tool_turns else 0
            if tool_calls_after - tool_calls_before != expected_delta:
                raise AssertionError(
                    f"turn {turn} did not execute the exact required tool-call count"
                )
            if turn in restart_turns:
                manifest = await harness.context_manifest()
                revision_before = manifest.revision
                observed_revision = max(observed_revision, revision_before)
                deterministic_model_calls += int(getattr(model, "invocations", 0))
                await _close_harness(harness, database, model_resource)
                harness, database, artifact_store, model, model_resource = _open_harness(
                    root,
                    max_context_tokens=max_context_tokens,
                    tool_turns=tool_turns,
                    tool_tracker=tool_tracker,
                    provider=provider,
                    provider_timeout=provider_timeout,
                )
                reopened = await harness.context_manifest()
                if reopened.revision != revision_before:
                    raise AssertionError("context manifest revision changed across reopen")
                reopen_count += 1

        deterministic_model_calls += int(getattr(model, "invocations", 0))
        if tool_tracker is not None and tuple(tool_tracker.calls) != tuple(sorted(tool_turns)):
            raise AssertionError("tool calls were missing, duplicated, or executed out of order")

        # A final public compaction makes the tail turn part of the immutable archive,
        # independent of where the automatic threshold happened to fall.
        await harness.compact_session(
            summary=(
                "Synthetic continuity plan remains active; exact facts stay in the "
                "scoped immutable trajectory archive."
            )
        )
        manifest = await harness.context_manifest()
        if manifest.revision <= observed_revision:
            raise AssertionError("final compaction did not advance the manifest")
        expected_sequences = tuple(range(1, manifest.revision + 1))
        if tuple(segment.sequence for segment in manifest.segments) != expected_sequences:
            raise AssertionError("archived segment sequence is not contiguous")
        if tuple(checkpoint.sequence for checkpoint in manifest.checkpoints) != expected_sequences:
            raise AssertionError("checkpoint sequence is not contiguous")
        if manifest.revision < 3:
            raise AssertionError("the probe did not exercise repeated compaction")

        selected_ids: list[str] = []
        for label in _MARKER_NAMES:
            query = markers[label][1]
            hits = await harness.search_session_context(query, limit=10)
            exact = [
                hit
                for hit in hits
                if hit.kind is ContextItemKind.USER_INTENT and query in hit.excerpt
            ]
            if len(exact) != 1:
                raise AssertionError(f"{label} continuity marker was not retrieved exactly once")
            selected_ids.append(exact[0].item_id)

        restored = await harness.rehydrate_session_context(selected_ids, max_tokens=4_000)
        restored_content = [item.content for item in restored.items]
        for label, content in zip(_MARKER_NAMES, restored_content, strict=True):
            if markers[label][1] not in content:
                raise AssertionError(f"{label} rehydration returned the wrong source item")
        injected = await harness.rehydrate_session_context(
            selected_ids,
            max_tokens=4_000,
            inject=True,
        )
        if not injected.injected:
            raise AssertionError("selective context rehydration was not persisted")

        tool_result_ids: list[str] = []
        if tool_tracker is not None:
            for label in _MARKER_NAMES:
                query = tool_markers[label][1]
                hits = await harness.search_session_context(query, limit=10)
                exact = [
                    hit
                    for hit in hits
                    if hit.kind is ContextItemKind.TOOL_RESULT and query in hit.excerpt
                ]
                if len(exact) != 1:
                    raise AssertionError(
                        f"{label} tool-result marker was not retrieved exactly once"
                    )
                tool_result_ids.append(exact[0].item_id)
            restored_tools = await harness.rehydrate_session_context(
                tool_result_ids,
                max_tokens=4_000,
            )
            restored_tool_content = [item.content for item in restored_tools.items]
            for label, content in zip(_MARKER_NAMES, restored_tool_content, strict=True):
                if tool_markers[label][1] not in content:
                    raise AssertionError(f"{label} tool result rehydrated the wrong item")

        budget = harness.inspect_context_budget()
        if budget is None or budget.used_tokens >= budget.max_tokens:
            raise AssertionError("live context was not bounded after rehydration")

        for segment in manifest.segments:
            await artifact_store.load_json(segment.artifact)
        manifest_text = json.dumps(manifest.to_dict(), sort_keys=True)
        if any(marker in manifest_text for _, marker in markers.values()):
            raise AssertionError("content leaked into the context manifest")

        revision_before_final_reopen = manifest.revision
        await _close_harness(harness, database, model_resource)
        harness, database, artifact_store, model, model_resource = _open_harness(
            root,
            max_context_tokens=max_context_tokens,
            tool_turns=tool_turns,
            tool_tracker=tool_tracker,
            provider=provider,
            provider_timeout=provider_timeout,
        )
        reopen_count += 1
        reopened = await harness.context_manifest()
        if reopened.revision != revision_before_final_reopen:
            raise AssertionError("final manifest did not survive database reopen")
        for label in _MARKER_NAMES:
            if not await harness.search_session_context(markers[label][1], limit=1):
                raise AssertionError(f"{label} marker was lost after final reopen")
            if tool_tracker is not None and not await harness.search_session_context(
                tool_markers[label][1], limit=1
            ):
                raise AssertionError(f"{label} tool-result marker was lost after final reopen")
        session = database.get_session(
            session_id="continuity-session",
            user_id="continuity-user",
        )
        runs = list(getattr(session, "runs", None) or []) if session is not None else []
        injection_runs = [
            run
            for run in runs
            if "agnoclaw_context_rehydration" in _run_metadata(run)
        ]
        if len(injection_runs) != 1:
            raise AssertionError("persisted rehydration evidence was not exactly-once")
        final_budget = harness.inspect_context_budget()
        if final_budget is None or final_budget.used_tokens >= final_budget.max_tokens:
            raise AssertionError("reopened live context exceeded its configured bound")

        return {
            "status": "passed",
            "protocol_version": _PROTOCOL_VERSION,
            "scope": (
                "deterministic-local-agno-sqlite-context-continuity"
                if provider.kind == "deterministic"
                else "live-ollama-agno-sqlite-context-continuity"
            ),
            "turns": turns,
            "scheduled_restarts": len(restart_turns),
            "verified_reopens": reopen_count,
            "compactions": reopened.revision,
            "automatic_compactions": max(0, reopened.revision - 1),
            "retrieval_checks": len(_MARKER_NAMES),
            "rehydrated_items": len(restored.items),
            "rehydration_persisted_exactly_once": True,
            "manifest_content_free": True,
            "artifact_integrity_checks": len(reopened.segments),
            "checkpoint_sequence_contiguous": True,
            "cross_process_lock": "local-file-rw-v1",
            "live_context_bounded": True,
            "final_context_tokens": final_budget.used_tokens,
            "max_context_tokens": final_budget.max_tokens,
            "provider": provider.kind,
            "provider_host_class": provider.host_class,
            "model_id": provider.model_id,
            "model_digest": provider.model_digest,
            "model_configuration_digest": provider.configuration_digest,
            "external_model_calls": 0 if provider.kind == "deterministic" else None,
            "external_model_calls_observed_minimum": (
                0 if provider.kind == "deterministic" else turns + len(tool_turns)
            ),
            "deterministic_model_calls": deterministic_model_calls,
            "tool_mode": (
                f"agno-native-{provider.kind}" if tool_tracker is not None else "disabled"
            ),
            "planned_tool_turns": len(tool_turns),
            "tool_calls": len(tool_tracker.calls) if tool_tracker is not None else 0,
            "tool_results_retrieved": len(tool_result_ids),
            "tool_results_rehydrated": len(tool_result_ids),
            "tool_results_exactly_once": tool_tracker is not None,
            "evidence_retained": evidence_retained,
        }
    finally:
        await _close_harness(harness, database, model_resource)


async def _main(args: argparse.Namespace) -> dict[str, Any]:
    restart_turns = _validate_args(args)
    provider = await _resolve_provider(args)
    context, retained = _evidence_context(args.evidence_dir)
    with context as directory:
        return await _exercise(
            Path(directory),
            turns=args.turns,
            max_context_tokens=args.max_context_tokens,
            restart_turns=restart_turns,
            tool_every=args.tool_every,
            evidence_retained=retained,
            provider=provider,
            provider_timeout=args.provider_timeout,
        )


def main() -> int:
    args = _parser().parse_args()
    try:
        # Agno provider logging is not part of the probe's strict JSON stdout protocol.
        with redirect_stdout(sys.stderr):
            report = asyncio.run(_main(args))
    except (ProbeConfigurationError, AssertionError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(report, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
