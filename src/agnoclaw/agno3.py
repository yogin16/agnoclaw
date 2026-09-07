"""Adapters for high-value Agno 3 capabilities.

The adapters keep version-sensitive imports and ownership decisions out of the
main harness. Agno remains responsible for its result/media formats; agnoclaw
decides when those formats may participate in its runtime and policy contracts.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import threading
from pathlib import Path
from typing import Any

from .compat import AgnoCompatibilityReport, AgnoFeature
from .runtime.errors import HarnessError

try:
    from agno.media.storage.base import MediaStorage as _MediaStorage
except ImportError:  # Agno 2.x compatibility lane

    class _MediaStorage:  # type: ignore[no-redef]
        pass


class LazyLocalMediaStorage(_MediaStorage):
    """Agno LocalMediaStorage that creates its directory only on first use."""

    backend_name = "local"

    def __init__(self, base_path: str | Path, *, persist_remote_urls: bool = False) -> None:
        self.base_path = Path(base_path).expanduser().resolve(strict=False)
        self.bucket = str(self.base_path)
        self.persist_remote_urls = persist_remote_urls
        self._storage: Any | None = None

    def _delegate(self) -> Any:
        if self._storage is None:
            from agno.media.storage.local import LocalMediaStorage

            self._storage = LocalMediaStorage(
                base_path=str(self.base_path),
                persist_remote_urls=self.persist_remote_urls,
            )
        return self._storage

    def upload(self, *args: Any, **kwargs: Any) -> str:
        return self._delegate().upload(*args, **kwargs)

    def download(self, *args: Any, **kwargs: Any) -> bytes:
        return self._delegate().download(*args, **kwargs)

    def get_url(self, *args: Any, **kwargs: Any) -> str | None:
        return self._delegate().get_url(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> bool:
        return bool(self._delegate().delete(*args, **kwargs))

    def exists(self, *args: Any, **kwargs: Any) -> bool:
        return bool(self._delegate().exists(*args, **kwargs))


class OwnedCodeModeResource:
    """Translate harness shutdown into CodeMode kernel termination."""

    def __init__(self, code_mode: Any) -> None:
        self.code_mode = code_mode

    def _stop_runner(self) -> None:
        # Agno 3 shutdown() kills kernels but intentionally keeps its loop
        # warm. Harness shutdown owns the whole CodeMode, so trigger Agno's own
        # finalizer after the sessions are empty to close that loop as well.
        runner = getattr(self.code_mode, "_runner", None)
        loop = getattr(runner, "_loop", None)
        thread = getattr(runner, "_thread", None)
        finalizer = getattr(self.code_mode, "_finalizer", None)
        if callable(finalizer) and getattr(finalizer, "alive", False):
            finalizer()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        if loop is not None and not loop.is_running() and not loop.is_closed():
            loop.close()

    async def _drain_snapshot_timers(self) -> None:
        """Cancel Agno's debounced snapshot tasks before its loop is closed.

        Agno 3.0.6 flushes live sessions during shutdown but leaves the separate
        debounce tasks registered. If the loop is then collected, Python 3.12+
        reports those coroutines as unraisable exceptions. The harness owns the
        CodeMode lifecycle, so it also owns draining these background tasks.
        """
        snapshots = getattr(self.code_mode, "_snapshots", None)
        timers = getattr(snapshots, "_timers", None)
        if not isinstance(timers, dict) or not timers:
            return
        pending = tuple(timers.values())
        timers.clear()
        for timer in pending:
            timer.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    def _drain_snapshot_timers_sync(self) -> None:
        runner = getattr(self.code_mode, "_runner", None)
        if not bool(getattr(runner, "started", False)):
            return
        run_on_loop = getattr(self.code_mode, "_run_on_loop_sync", None)
        if callable(run_on_loop):
            run_on_loop(self._drain_snapshot_timers())

    def close(self) -> None:
        self._drain_snapshot_timers_sync()
        self.code_mode.shutdown()
        self._stop_runner()

    async def aclose(self) -> None:
        runner = getattr(self.code_mode, "_runner", None)
        run_on_loop = getattr(self.code_mode, "_run_on_loop", None)
        if bool(getattr(runner, "started", False)) and callable(run_on_loop):
            await run_on_loop(self._drain_snapshot_timers())
        await self.code_mode.ashutdown()
        self._stop_runner()


def resolve_tool_result_offloading(
    *,
    explicit: Any | None,
    mode: str,
    threshold_chars: int,
    ttl_seconds: int | None,
    compression_enabled: bool,
    governed_spill_enabled: bool,
    db: Any,
    compatibility: AgnoCompatibilityReport,
) -> Any | None:
    """Resolve Agno ResultStore settings without duplicating another spill owner."""
    requested = explicit is not None or mode == "enabled"
    if explicit is False or (explicit is None and mode == "disabled"):
        return None
    if explicit is None and mode == "auto":
        if not compatibility.has(AgnoFeature.V3_TOOL_RESULT_OFFLOADING):
            return None
        if compression_enabled or governed_spill_enabled or db is None:
            return None
        requested = True
    if not requested:
        return None
    compatibility.require(AgnoFeature.V3_TOOL_RESULT_OFFLOADING)
    if compression_enabled or governed_spill_enabled:
        owner = "compression" if compression_enabled else "agnoclaw governed-output spill"
        raise HarnessError(
            code="AGNO_TOOL_RESULT_OFFLOAD_CONFLICT",
            category="configuration",
            message=f"Agno tool-result offloading cannot be combined with {owner}.",
            retryable=False,
            details={"owner": owner},
        )
    if explicit is not None and explicit is not True:
        return explicit
    from agno.offload.store import ResultStore

    return ResultStore(
        threshold_chars=threshold_chars,
        ttl_seconds=ttl_seconds,
    )


def resolve_media_storage(
    *,
    explicit: Any | None,
    mode: str,
    local_path: str | Path,
    persist_remote_urls: bool,
    persistent_sqlite: bool,
    compatibility: AgnoCompatibilityReport,
) -> Any | None:
    """Resolve explicit cloud storage or lazy local media storage."""
    if explicit is not None:
        compatibility.require(AgnoFeature.V3_MEDIA_OFFLOADING)
        return explicit
    if mode == "disabled":
        return None
    if mode == "auto":
        if not persistent_sqlite or not compatibility.has(AgnoFeature.V3_MEDIA_OFFLOADING):
            return None
    else:
        compatibility.require(AgnoFeature.V3_MEDIA_OFFLOADING)
    return LazyLocalMediaStorage(
        local_path,
        persist_remote_urls=persist_remote_urls,
    )


def resolve_agno3_runtime(
    *,
    explicit_tool_result: Any | None,
    explicit_media: Any | None,
    explicit_code_mode: Any | None,
    config: Any,
    provided_db: Any | None,
    db: Any,
    tools: list[Any],
    profile: str,
    owner_scope: tuple[str | None, str | None, str | None],
    cwd: str | Path,
    compression_enabled: bool,
    governed_spill_enabled: bool,
    compatibility: AgnoCompatibilityReport,
) -> tuple[Any | None, Any | None, Any | None, OwnedCodeModeResource | None]:
    """Resolve all opt-in Agno 3 resources behind one version boundary."""
    persistent_sqlite = bool(
        config.storage.backend == "sqlite"
        and config.storage.sqlite_path != ":memory:"
        and (provided_db is None or type(db).__module__.startswith("agno.db.sqlite"))
    )
    media = resolve_media_storage(
        explicit=explicit_media,
        mode=config.agno_media_offloading,
        local_path=config.agno_media_storage_path,
        persist_remote_urls=config.agno_media_persist_remote_urls,
        persistent_sqlite=persistent_sqlite,
        compatibility=compatibility,
    )
    tool_results = resolve_tool_result_offloading(
        explicit=explicit_tool_result,
        mode=config.agno_tool_result_offloading,
        threshold_chars=config.agno_tool_result_threshold_chars,
        ttl_seconds=config.agno_tool_result_ttl_seconds,
        compression_enabled=compression_enabled,
        governed_spill_enabled=governed_spill_enabled,
        db=db,
        compatibility=compatibility,
    )
    scope_key = hashlib.sha256(repr(owner_scope).encode()).hexdigest()[:24]
    code_mode, owned_code_mode = resolve_code_mode(
        explicit=explicit_code_mode,
        enabled=config.enable_code_mode,
        tools=tools,
        db=db,
        profile=profile,
        scope_key=scope_key,
        cwd=cwd,
        allow_shell=config.code_mode_allow_shell,
        snapshot=config.code_mode_snapshot,
        timeout_seconds=config.code_mode_timeout_seconds,
        idle_ttl_seconds=config.code_mode_idle_ttl_seconds,
        max_kernels=config.code_mode_max_kernels,
        compatibility=compatibility,
    )
    return tool_results, media, code_mode, owned_code_mode


def describe_agno3_runtime(
    *,
    tool_results: Any | None,
    media: Any | None,
    code_mode: Any | None,
    compatibility: AgnoCompatibilityReport,
) -> dict[str, Any]:
    """Return content-minimized Agno 3 settings for the harness manifest."""
    return {
        "context": {
            "agno_incremental_history": compatibility.has(AgnoFeature.V3_INCREMENTAL_HISTORY)
        },
        "output": {
            "agno_tool_result_offloading": tool_results is not None,
            "agno_tool_result_threshold_chars": getattr(tool_results, "threshold_chars", None),
            "agno_media_offloading": media is not None,
            "agno_media_backend": getattr(media, "backend_name", None),
        },
        "code_mode": {
            "enabled": code_mode is not None,
            "allow_shell": getattr(code_mode, "allow_shell", None),
            "snapshot": getattr(code_mode, "snapshot", None),
            "kernel_plane": "host_process" if code_mode is not None else None,
            "tool_handle_plane": "runtime_backend" if code_mode is not None else None,
            "security_sandbox": False if code_mode is not None else None,
        },
    }


def resolve_code_mode(
    *,
    explicit: Any | None,
    enabled: bool,
    tools: list[Any],
    db: Any,
    profile: str,
    scope_key: str,
    cwd: str | Path,
    allow_shell: bool,
    snapshot: bool,
    timeout_seconds: int,
    idle_ttl_seconds: int,
    max_kernels: int,
    compatibility: AgnoCompatibilityReport,
) -> tuple[Any | None, OwnedCodeModeResource | None]:
    """Build an Agno CodeMode with bounded, shell-disabled defaults."""
    if explicit is False or (explicit is None and not enabled):
        return None, None
    if profile not in {"legacy", "quick"}:
        raise HarnessError(
            code="AGNO_CODE_MODE_PROFILE_UNSUPPORTED",
            category="configuration",
            message=(
                "CodeMode currently supports quick and legacy profiles only; "
                "durable/service runs require run-owned kernel recovery first."
            ),
            retryable=False,
            details={"profile": profile},
        )
    compatibility.require(AgnoFeature.V3_CODE_MODE)
    try:
        from agno.tools.code import CodeMode
    except ImportError as exc:  # pragma: no cover - guarded by the capability probe
        raise HarnessError(
            code="AGNO_CODE_MODE_DEPENDENCY_MISSING",
            category="configuration",
            message="CodeMode requires the agnoclaw[code] extra.",
            retryable=False,
            details={"install": "pip install 'agnoclaw[code]'"},
        ) from exc
    if explicit is not None and explicit is not True:
        if not isinstance(explicit, CodeMode):
            raise TypeError("code_mode must be a bool or agno.tools.code.CodeMode")
        return explicit, None

    class ScopedCodeMode(CodeMode):
        """Keep kernels/snapshots isolated by harness owner and Agno user."""

        def _scoped_context(self, run_context: Any) -> Any:
            scoped = copy.copy(run_context)
            material = "\0".join(
                (
                    scope_key,
                    str(getattr(run_context, "user_id", None) or ""),
                    str(run_context.session_id),
                )
            )
            scoped.session_id = "scope_" + hashlib.sha256(material.encode()).hexdigest()[:32]
            return scoped

        def execute(
            self,
            run_context: Any,
            code: str,
            agent: Any = None,
            team: Any = None,
        ) -> Any:
            return super().execute(self._scoped_context(run_context), code, agent, team)

        async def aexecute(
            self,
            run_context: Any,
            code: str,
            agent: Any = None,
            team: Any = None,
        ) -> Any:
            return await super().aexecute(self._scoped_context(run_context), code, agent, team)

        def restart(self, run_context: Any) -> str:
            return super().restart(self._scoped_context(run_context))

        async def arestart(self, run_context: Any) -> str:
            return await super().arestart(self._scoped_context(run_context))

    filesystem = None
    if snapshot and db is not None:
        from agno.fs import FileSystem

        filesystem = FileSystem(
            backend=db,
            namespace=f"agnoclaw-code-mode-{scope_key}",
        )
    code_mode = ScopedCodeMode(
        tools=tools,
        fs=filesystem,
        snapshot=bool(snapshot and filesystem is not None),
        allow_shell=allow_shell,
        timeout=timeout_seconds,
        idle_ttl=idle_ttl_seconds,
        max_kernels=max_kernels,
        cwd=str(Path(cwd).resolve(strict=False)),
    )
    return code_mode, OwnedCodeModeResource(code_mode)
