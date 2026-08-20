"""Run-owned materialization for the first-party local built-in tool suite."""

from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..backends import RuntimeBackend
from ..config import HarnessConfig, RuntimeProfile
from ..session_commands import _ElevatedSessionCommandExecutor
from ..tools import get_default_tools
from ..tools.backends import CommandExecutor, LocalCommandExecutor, SandboxMode
from .tool_ingress import builtin_effect_manifest


def can_materialize_host_builtin_tools(
    *,
    profile: RuntimeProfile,
    config: HarnessConfig,
    backend: RuntimeBackend,
    subagents: dict[str, Any] | None,
) -> bool:
    """Return whether the built-in surface has a certified local run factory."""
    return (
        profile in {RuntimeProfile.QUICK, RuntimeProfile.LEGACY}
        and backend.uses_host_runtime()
        and not subagents
        and not config.enable_background_bash_tools
        and not config.enable_browser
        and not config.enable_media_tools
        and not config.enable_notebook_tools
    )


def builtin_tool_settings(
    *,
    enabled: bool,
    config: HarnessConfig,
    backend: RuntimeBackend,
    subagents: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return content-minimized inputs that change the built-in tool surface."""
    mcp_digest = hashlib.sha256(
        json.dumps(config.mcp_servers, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    subagent_manifest = {
        str(name): {
            "description": getattr(definition, "description", ""),
            "prompt": getattr(definition, "prompt", ""),
            "tools": list(getattr(definition, "tools", ()) or ()),
            "model": getattr(definition, "model", None),
        }
        for name, definition in sorted((subagents or {}).items())
    }
    subagent_digest = hashlib.sha256(
        json.dumps(subagent_manifest, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return {
        "enabled": enabled,
        "backend_type": f"{type(backend).__module__}.{type(backend).__qualname__}",
        "host_runtime": backend.uses_host_runtime(),
        "bash": config.enable_bash,
        "bash_timeout_seconds": config.bash_timeout_seconds,
        "background_bash": config.enable_background_bash_tools,
        "web_search": config.enable_web_search,
        "web_fetch": config.enable_web_fetch,
        "browser": config.enable_browser,
        "mcp_server_count": len(config.mcp_servers),
        "mcp_digest": mcp_digest,
        "media": config.enable_media_tools,
        "notebook": config.enable_notebook_tools,
        "named_subagents": sorted(subagent_manifest),
        "subagent_digest": subagent_digest,
        "subagent_default_model": config.default_model,
        "effect_manifest": builtin_effect_manifest(),
    }


def _resource_owner(resource: Any) -> Any:
    entrypoint = getattr(resource, "entrypoint", None)
    return getattr(entrypoint, "__self__", None) or resource


@dataclass
class BuiltinToolBundle:
    """One run's tools plus every resource whose lifetime ends with that run."""

    tools: tuple[Any, ...]
    resources: tuple[Any, ...] = field(repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def _claim_resources(self) -> tuple[Any, ...]:
        if self._closed:
            return ()
        self._closed = True
        seen: set[int] = set()
        claimed: list[Any] = []
        for candidate in reversed(self.resources):
            resource = _resource_owner(candidate)
            identity = id(resource)
            if identity in seen:
                continue
            seen.add(identity)
            claimed.append(resource)
        return tuple(claimed)

    def close(self) -> None:
        """Release run-owned resources once, in reverse acquisition order."""
        for resource in self._claim_resources():
            closer = getattr(resource, "close", None)
            if callable(closer):
                closer()

    async def aclose(self) -> None:
        """Release sync or async resources once without leaking awaitables."""
        for resource in self._claim_resources():
            async_closer = getattr(resource, "aclose", None)
            if callable(async_closer):
                result = async_closer()
                if inspect.isawaitable(result):
                    await result
                continue
            closer = getattr(resource, "close", None)
            if callable(closer):
                closer()


def materialize_host_builtin_tools(
    context: Any,
    *,
    config: HarnessConfig,
    subagents: dict[str, Any] | None,
    workspace_dir: str | Path,
    sandbox_dir: str | Path | None,
    sandbox_mode: SandboxMode,
    harness: Any,
) -> BuiltinToolBundle:
    """Build the supported host-local built-ins without sharing mutable objects."""
    del context
    backend = RuntimeBackend()
    host_executor = LocalCommandExecutor(workspace_dir=workspace_dir)
    resources: list[Any] = [host_executor]

    def wrap_command_executor(executor: CommandExecutor) -> CommandExecutor:
        wrapper = _ElevatedSessionCommandExecutor(
            harness=harness,
            sandbox_executor=executor,
            host_executor=host_executor,
            owns_sandbox_executor=True,
        )
        resources.append(wrapper)
        return wrapper

    tools = tuple(
        get_default_tools(
            config,
            subagents=subagents,
            workspace_dir=workspace_dir,
            sandbox_dir=sandbox_dir,
            sandbox_mode=sandbox_mode,
            backend=backend,
            command_executor_wrapper=wrap_command_executor,
            network_policy=harness._guardrails,
        )
    )
    resources.extend(tools)
    return BuiltinToolBundle(tools=tools, resources=tuple(resources))


__all__ = [
    "BuiltinToolBundle",
    "builtin_tool_settings",
    "can_materialize_host_builtin_tools",
    "materialize_host_builtin_tools",
]
