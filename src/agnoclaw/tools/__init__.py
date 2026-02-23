"""
Default tool suite for agnoclaw.

Usage:
    from agnoclaw.tools import get_default_tools

    tools = get_default_tools(config)
    agent = HarnessAgent(tools=tools, ...)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from agnoclaw.config import HarnessConfig

from .bash import make_bash_tool
from .files import FilesToolkit
from .tasks import ProgressToolkit, TodoToolkit, make_subagent_tool
from .web import WebToolkit

__all__ = [
    "make_bash_tool",
    "FilesToolkit",
    "WebToolkit",
    "TodoToolkit",
    "ProgressToolkit",
    "make_subagent_tool",
    "get_default_tools",
]


def get_default_tools(config: Optional["HarnessConfig"] = None) -> list:
    """
    Build the default tool suite based on configuration.

    Returns a list of tools and toolkits ready to pass to HarnessAgent.
    """
    from agnoclaw.config import get_config

    cfg = config or get_config()
    tools = []

    # File operations (always enabled)
    tools.append(FilesToolkit())

    # Shell execution
    if cfg.enable_bash:
        tools.append(make_bash_tool(timeout=cfg.bash_timeout_seconds))

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

    # Sub-agent spawning
    tools.append(make_subagent_tool(default_model=cfg.default_model))

    return tools
