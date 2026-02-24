"""
Default tool suite for agnoclaw.

Usage:
    from agnoclaw.tools import get_default_tools

    tools = get_default_tools(config)
    agent = AgentHarness(tools=tools, ...)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from agnoclaw.config import HarnessConfig

from .bash import BashToolkit, make_bash_tool
from .files import FilesToolkit
from .tasks import ProgressToolkit, SubagentDefinition, TodoToolkit, make_subagent_tool
from .web import WebToolkit

__all__ = [
    "make_bash_tool",
    "BashToolkit",
    "FilesToolkit",
    "WebToolkit",
    "TodoToolkit",
    "ProgressToolkit",
    "make_subagent_tool",
    "SubagentDefinition",
    "get_default_tools",
]


def get_default_tools(
    config: Optional["HarnessConfig"] = None,
    subagents: Optional[dict[str, SubagentDefinition]] = None,
) -> list:
    """
    Build the default tool suite based on configuration.

    Args:
        config: HarnessConfig for tool settings.
        subagents: Named subagent definitions to register with the SubagentTool.

    Returns a list of tools and toolkits ready to pass to AgentHarness.
    """
    from agnoclaw.config import get_config

    cfg = config or get_config()
    tools = []

    # File operations (always enabled)
    tools.append(FilesToolkit())

    # Shell execution
    if cfg.enable_bash:
        if cfg.enable_background_bash_tools:
            tools.append(BashToolkit(timeout=cfg.bash_timeout_seconds, workspace_dir=cfg.workspace_dir))
        else:
            tools.append(make_bash_tool(timeout=cfg.bash_timeout_seconds, workspace_dir=cfg.workspace_dir))

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
    tools.append(ProgressToolkit())

    # Sub-agent spawning (with optional named agents)
    tools.append(make_subagent_tool(
        default_model=cfg.default_model,
        subagents=subagents,
    ))

    return tools
