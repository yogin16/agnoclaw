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
    from agnoclaw.config import HarnessConfig

from .backends import CommandExecutor, LocalCommandExecutor, LocalWorkspaceAdapter, WorkspaceAdapter
from .bash import BashToolkit, make_bash_tool
from .files import FilesToolkit
from .tasks import ProgressToolkit, SubagentDefinition, TodoToolkit, make_subagent_tool
from .web import WebToolkit

logger = logging.getLogger("agnoclaw.tools")

__all__ = [
    "make_bash_tool",
    "BashToolkit",
    "CommandExecutor",
    "FilesToolkit",
    "LocalCommandExecutor",
    "LocalWorkspaceAdapter",
    "WorkspaceAdapter",
    "WebToolkit",
    "TodoToolkit",
    "ProgressToolkit",
    "make_subagent_tool",
    "SubagentDefinition",
    "get_default_tools",
]


def get_default_tools(
    config: HarnessConfig | None = None,
    subagents: dict[str, SubagentDefinition] | None = None,
    workspace_dir: str | Path | None = None,
    command_executor: CommandExecutor | None = None,
    workspace_adapter: WorkspaceAdapter | None = None,
) -> list:
    """
    Build the default tool suite based on configuration.

    Args:
        config: HarnessConfig for tool settings.
        subagents: Named subagent definitions to register with the SubagentTool.
        workspace_dir: Explicit workspace root for filesystem/shell/project tools.

    Returns a list of tools and toolkits ready to pass to AgentHarness.
    """
    from agnoclaw.config import get_config

    cfg = config or get_config()
    tool_workspace_dir = (
        Path(workspace_dir).expanduser().resolve()
        if workspace_dir is not None
        else Path(cfg.workspace_dir).expanduser().resolve()
    )
    tools = []

    # File operations (always enabled)
    resolved_workspace_adapter = workspace_adapter or LocalWorkspaceAdapter(
        workspace_dir=tool_workspace_dir
    )
    resolved_command_executor = command_executor or LocalCommandExecutor(
        workspace_dir=tool_workspace_dir
    )

    tools.append(
        FilesToolkit(
            workspace_dir=tool_workspace_dir,
            adapter=resolved_workspace_adapter,
        )
    )

    # Shell execution
    if cfg.enable_bash:
        if cfg.enable_background_bash_tools:
            tools.append(
                BashToolkit(
                    timeout=cfg.bash_timeout_seconds,
                    workspace_dir=tool_workspace_dir,
                    executor=resolved_command_executor,
                )
            )
        else:
            tools.append(
                make_bash_tool(
                    timeout=cfg.bash_timeout_seconds,
                    workspace_dir=tool_workspace_dir,
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
        config=cfg,
        command_executor=resolved_command_executor,
        workspace_adapter=resolved_workspace_adapter,
    ))

    # ── Optional toolkits (conditional on config + importability) ─────────

    # Browser toolkit (requires agnoclaw[browser])
    if cfg.enable_browser:
        try:
            from .browser import BrowserToolkit
            tools.append(BrowserToolkit())
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
