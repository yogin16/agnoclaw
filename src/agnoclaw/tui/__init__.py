"""
agnoclaw TUI — Textual-based terminal user interface.

Requires the `tui` extra: pip install agnoclaw[tui]
"""

try:
    from .app import AgnoClawApp
except ImportError as e:
    raise ImportError(
        "TUI dependencies not installed. Install with: pip install agnoclaw[tui]"
    ) from e

__all__ = ["AgnoClawApp"]
