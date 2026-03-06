"""
HeaderBar widget — top bar showing model name and session info.
"""

from __future__ import annotations

from pathlib import Path

from textual.widgets import Static


class HeaderBar(Static):
    """Top bar displaying model and session information."""

    DEFAULT_CSS = """
    HeaderBar {
        dock: top;
        height: 1;
        background: #0d0d10;
        color: #6f6f79;
        padding: 0 1;
        border: none;
    }
    """

    def __init__(
        self,
        *,
        model: str = "",
        session_id: str | None = None,
        workspace_path: str | None = None,
        **kwargs,
    ) -> None:
        self._model = model
        self._session_id = session_id
        self._workspace_path = workspace_path or ""
        super().__init__(self._build_text(), **kwargs)

    def _build_text(self) -> str:
        parts = ["agnoclaw"]
        if self._model:
            parts.append(self._model)
        if self._workspace_path:
            short = Path(self._workspace_path).name or self._workspace_path
            parts.append(short)
        if self._session_id:
            short = self._session_id[:8] if len(self._session_id) > 8 else self._session_id
            parts.append(f"session {short}")
        parts.append("? shortcuts")
        return "  ·  ".join(parts)

    def update_info(
        self,
        *,
        model: str | None = None,
        session_id: str | None = None,
        workspace_path: str | None = None,
    ) -> None:
        if model is not None:
            self._model = model
        if session_id is not None:
            self._session_id = session_id
        if workspace_path is not None:
            self._workspace_path = workspace_path
        self.update(self._build_text())
