"""
Default tool suite for agnoclaw.

Usage:
    from agnoclaw.tools import get_default_tools

    tools = get_default_tools(config)
    agent = AgentHarness(tools=tools, ...)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agnoclaw.backends import RuntimeBackend
    from agnoclaw.config import HarnessConfig

from .backends import (
    CommandExecutor,
    LocalCommandExecutor,
    LocalWorkspaceAdapter,
    WorkspaceAdapter,
    bind_session_sandbox,
)
from .bash import BashToolkit, make_bash_tool
from .browser_backends import BrowserBackend, LocalPlaywrightBrowserBackend
from .files import FilesToolkit
from .tasks import ProgressToolkit, SubagentDefinition, TodoToolkit, make_subagent_tool
from .web import WebToolkit

logger = logging.getLogger("agnoclaw.tools")

__all__ = [
    "make_bash_tool",
    "BashToolkit",
    "BrowserBackend",
    "CommandExecutor",
    "FilesToolkit",
    "LocalPlaywrightBrowserBackend",
    "LocalCommandExecutor",
    "LocalWorkspaceAdapter",
    "RuntimeBackend",
    "WorkspaceAdapter",
    "WebToolkit",
    "TodoToolkit",
    "ProgressToolkit",
    "make_subagent_tool",
    "SubagentDefinition",
    "get_default_tools",
]


def __getattr__(name: str):
    if name == "RuntimeBackend":
        from agnoclaw.backends import RuntimeBackend

        return RuntimeBackend
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def get_default_tools(
    config: HarnessConfig | None = None,
    subagents: dict[str, SubagentDefinition] | None = None,
    workspace_dir: str | Path | None = None,
    sandbox_dir: str | Path | None = None,
    backend: RuntimeBackend | None = None,
) -> list:
    """
    Build the default tool suite based on configuration.

    Args:
        config: HarnessConfig for tool settings.
        subagents: Named subagent definitions to register with the SubagentTool.
        workspace_dir: Explicit workspace root for filesystem/shell/project tools.
        sandbox_dir: Optional session-scoped scratch root for files/bash tools.

    Returns a list of tools and toolkits ready to pass to AgentHarness.
    """
    from agnoclaw.backends import RuntimeBackend
    from agnoclaw.config import get_config

    cfg = config or get_config()

    tool_workspace_dir = (
        Path(workspace_dir).expanduser().resolve()
        if workspace_dir is not None
        else Path(cfg.workspace_dir).expanduser().resolve()
    )
    tool_sandbox_dir = (
        Path(sandbox_dir).expanduser().resolve()
        if sandbox_dir is not None
        else None
    )
    effective_backend = backend or RuntimeBackend()
    tools = []
    resolved_backend = effective_backend.resolve(workspace_dir=tool_workspace_dir)
    resolved_command_executor, resolved_workspace_adapter = bind_session_sandbox(
        command_executor=resolved_backend.command_executor,
        workspace_adapter=resolved_backend.workspace_adapter,
        workspace_dir=tool_workspace_dir,
        sandbox_dir=tool_sandbox_dir,
    )
    resolved_browser_backend = resolved_backend.browser_backend

    tools.append(
        FilesToolkit(
            workspace_dir=tool_sandbox_dir or tool_workspace_dir,
            adapter=resolved_workspace_adapter,
        )
    )

    # Shell execution
    if cfg.enable_bash:
        if cfg.enable_background_bash_tools:
            tools.append(
                BashToolkit(
                    timeout=cfg.bash_timeout_seconds,
                    workspace_dir=tool_sandbox_dir or tool_workspace_dir,
                    executor=resolved_command_executor,
                )
            )
        else:
            tools.append(
                make_bash_tool(
                    timeout=cfg.bash_timeout_seconds,
                    workspace_dir=tool_sandbox_dir or tool_workspace_dir,
                    executor=resolved_command_executor,
                )
            )

    # Web tools
    tools.append(
        WebToolkit(
            search_enabled=cfg.enable_web_search,
            fetch_enabled=cfg.enable_web_fetch,
        )
    )

    # Planning (always enabled — context engineering)
    tools.append(TodoToolkit())

    # Multi-window project tracking (always enabled)
    tools.append(ProgressToolkit(project_dir=tool_workspace_dir))

    # Sub-agent spawning (with optional named agents)
    tools.append(make_subagent_tool(
        default_model=cfg.default_model,
        subagents=subagents,
        workspace_dir=tool_workspace_dir,
        sandbox_dir=tool_sandbox_dir,
        config=cfg,
        backend=effective_backend,
    ))

    # ── Optional toolkits (conditional on config + importability) ─────────

    # Browser toolkit (requires agnoclaw[browser])
    if cfg.enable_browser:
        if resolved_browser_backend is None and not effective_backend.uses_host_runtime():
            raise ValueError(
                "Browser tools are enabled, but the configured backend does not "
                "provide browser support. "
                "Pass a browser-capable backend or disable browser tools."
            )
        try:
            from .browser import BrowserToolkit
            if resolved_browser_backend is None:
                tools.append(BrowserToolkit())
            else:
                tools.append(BrowserToolkit(backend=resolved_browser_backend))
            logger.debug("Browser toolkit enabled")
        except ImportError:
            logger.debug("Browser toolkit requested but playwright not installed")

    # MCP toolkits (one per configured server)
    for server_cfg in cfg.mcp_servers:
        try:
            from .mcp import MCPToolkit

            toolkit = MCPToolkit(
                name=server_cfg.get("name", "mcp"),
                command=server_cfg.get("command"),
                url=server_cfg.get("url"),
                env=server_cfg.get("env"),
            )
            # Defer connection — connect on first tool call or explicitly
            tools.append(toolkit)
            logger.debug("MCP toolkit configured: %s", server_cfg.get("name", "mcp"))
        except ImportError:
            logger.debug("MCP toolkit requested but mcp package not installed")
            break

    # Media toolkit (requires agnoclaw[media])
    if cfg.enable_media_tools:
        try:
            from .media import MediaToolkit
            tools.append(MediaToolkit())
            logger.debug("Media toolkit enabled")
        except ImportError:
            logger.debug("Media toolkit requested but dependencies not installed")

    # Notebook toolkit (nbformat or raw JSON fallback)
    if cfg.enable_notebook_tools:
        from .notebook import NotebookToolkit
        tools.append(NotebookToolkit())
        logger.debug("Notebook toolkit enabled")

    return tools
