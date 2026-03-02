"""
HeaderBar widget — top bar showing model name and session info.
"""

from __future__ import annotations

from textual.widgets import Static


class HeaderBar(Static):
    """Top bar displaying model and session information."""

    DEFAULT_CSS = """
    HeaderBar {
        dock: top;
        height: 1;
        background: $accent;
        color: $text;
        padding: 0 1;
        text-style: bold;
    }
    """

    def __init__(
        self,
        *,
        model: str = "",
        session_id: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._model = model
        self._session_id = session_id

    def on_mount(self) -> None:
        self._render()

    def update_info(
        self,
        *,
        model: str | None = None,
        session_id: str | None = None,
    ) -> None:
        if model is not None:
            self._model = model
        if session_id is not None:
            self._session_id = session_id
        self._render()

    def _render(self) -> None:
        parts = ["agnoclaw"]
        if self._model:
            parts.append(f"· {self._model}")
        if self._session_id:
            short = self._session_id[:8] if len(self._session_id) > 8 else self._session_id
            parts.append(f"· session:{short}")
        self.update(" ".join(parts))
