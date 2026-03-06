"""Unit tests for slash-command handling in the Textual TUI app."""

from __future__ import annotations

import pytest

pytest.importorskip("textual")

from agnoclaw.tui.app import AgnoClawApp
from agnoclaw.tui.events import UserSubmitted


class _DummySkills:
    def __init__(self, skills: list[dict]) -> None:
        self._skills = skills

    def list_skills(self) -> list[dict]:
        return self._skills


class _DummyAgent:
    def __init__(self) -> None:
        self.session_id = "session-old"
        self.skills = _DummySkills(
            [
                {"name": "code-review", "description": "Review code quality"},
                {"name": "git-workflow", "description": "Plan git steps"},
            ]
        )

    def clear_session_context(self) -> str:
        self.session_id = "session-new-123"
        return self.session_id

    async def compact_session(self) -> None:
        return None


class _DummyChat:
    def __init__(self) -> None:
        self.notifications: list[tuple[str, str]] = []
        self.errors: list[str] = []
        self.cleared = False
        self.welcome_calls = 0

    def add_notification(self, text: str, *, style: str = "yellow") -> None:
        self.notifications.append((text, style))

    def add_error(self, error: str) -> None:
        self.errors.append(error)

    def clear_log(self) -> None:
        self.cleared = True

    def add_welcome_banner(self, *, model: str = "", session_id: str | None = None) -> None:
        del model, session_id
        self.welcome_calls += 1

    def add_startup_note(self, *, model: str = "", session_id: str | None = None) -> None:
        del model, session_id
        self.welcome_calls += 1

    def start_working(self, title: str) -> None:
        del title

    def update_working(self, title: str, elapsed_s: int) -> None:
        del title, elapsed_s

    def finish_working(self, *, success: bool, elapsed_s: int) -> None:
        del success, elapsed_s


class _DummyInputBar:
    def __init__(self) -> None:
        self.disabled = False
        self.value = ""
        self.focused = False

    def set_disabled(self, disabled: bool) -> None:
        self.disabled = disabled

    def focus(self) -> None:
        self.focused = True


class _DummyStatusBar:
    def __init__(self) -> None:
        self.compacting = False
        self.skill: str | None = None
        self.agent_status: str = "ready"

    def set_compacting(self, compacting: bool) -> None:
        self.compacting = compacting

    def set_queued_skill(self, skill_name: str | None) -> None:
        self.skill = skill_name

    def set_agent_status(self, status_text: str) -> None:
        self.agent_status = status_text


class _DummyAssistBar:
    def __init__(self) -> None:
        self.skill: str | None = None
        self.value = ""

    def set_queued_skill(self, skill_name: str | None) -> None:
        self.skill = skill_name

    def update_for_input(self, value: str) -> None:
        self.value = value

    def toggle_shortcuts(self) -> None:
        return


class _DummyNotif:
    def __init__(self) -> None:
        self.notes: list[tuple[str, str]] = []

    def add_system_note(self, note: str, *, style: str = "cyan") -> None:
        self.notes.append((note, style))


def test_skill_list_alias_from_skill_command(monkeypatch):
    app = AgnoClawApp(agent=_DummyAgent())
    chat = _DummyChat()

    monkeypatch.setattr(
        app,
        "query_one",
        lambda selector, *_args, **_kwargs: {"#chat-log": chat}[selector],
    )

    app._handle_slash_command("/skill list")

    assert any("Available skills:" in text for text, _style in chat.notifications)
    assert not any("Skill not found: list" in text for text, _style in chat.notifications)


def test_clear_resets_startup_context_with_new_session(monkeypatch):
    app = AgnoClawApp(agent=_DummyAgent())
    chat = _DummyChat()
    status = _DummyStatusBar()
    assist = _DummyAssistBar()
    notif = _DummyNotif()
    app._startup_context_shown = True

    monkeypatch.setattr(
        app,
        "query_one",
        lambda selector, *_args, **_kwargs: {
            "#chat-log": chat,
            "#status-bar": status,
            "#assist-bar": assist,
            "#notif-panel": notif,
        }[selector],
    )

    app._handle_slash_command("/clear")

    assert chat.cleared is True
    assert app._startup_context_shown is False
    assert any("New session: session-new-123" in text for text, _style in chat.notifications)


def test_compact_starts_background_worker(monkeypatch):
    app = AgnoClawApp(agent=_DummyAgent())
    chat = _DummyChat()
    input_bar = _DummyInputBar()
    status = _DummyStatusBar()
    captured = []

    monkeypatch.setattr(
        app,
        "query_one",
        lambda selector, *_args, **_kwargs: {
            "#chat-log": chat,
            "#input-bar": input_bar,
            "#status-bar": status,
        }[selector],
    )

    def _capture_worker(coro, **kwargs):
        captured.append((coro, kwargs))
        return None

    monkeypatch.setattr(app, "run_worker", _capture_worker)

    app._handle_slash_command("/compact")

    assert app._compaction_running is True
    assert input_bar.disabled is True
    assert status.compacting is True
    assert any("Compacting session..." in text for text, _style in chat.notifications)
    assert len(captured) == 1

    coro, _worker_kwargs = captured[0]
    coro.close()


def test_theme_command_switches_named_theme(monkeypatch):
    app = AgnoClawApp(agent=_DummyAgent())
    chat = _DummyChat()
    applied: list[str] = []

    monkeypatch.setattr(
        app,
        "query_one",
        lambda selector, *_args, **_kwargs: {"#chat-log": chat}[selector],
    )
    monkeypatch.setattr(
        app,
        "_apply_ui_theme",
        lambda theme_name, announce=True: applied.append(theme_name),
    )

    app._handle_slash_command("/theme sunset")

    assert applied == ["sunset"]


def test_slash_question_mark_toggles_shortcuts(monkeypatch):
    app = AgnoClawApp(agent=_DummyAgent())
    toggled: list[bool] = []

    monkeypatch.setattr(app, "action_toggle_shortcuts", lambda: toggled.append(True))

    app._handle_slash_command("/?")

    assert toggled == [True]


def test_submit_while_streaming_preserves_input(monkeypatch):
    app = AgnoClawApp(agent=_DummyAgent())
    app._agent_driver._streaming = True
    chat = _DummyChat()
    input_bar = _DummyInputBar()
    status = _DummyStatusBar()

    monkeypatch.setattr(
        app,
        "query_one",
        lambda selector, *_args, **_kwargs: {
            "#chat-log": chat,
            "#input-bar": input_bar,
            "#status-bar": status,
        }[selector],
    )

    app.on_user_submitted(UserSubmitted("continue typing"))

    assert input_bar.value == "continue typing"
    assert input_bar.focused is True
    assert any("still working" in text.lower() for text, _style in chat.notifications)
