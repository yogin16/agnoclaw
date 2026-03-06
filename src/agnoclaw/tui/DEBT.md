# TUI Debt / Next Session Resume Notes

This file tracks high-impact UX improvements to continue after the current TUI revamp commit.

## Priority Backlog

1. Inline tool traces in chat (Codex/Claude-like)
- Render tool events directly in transcript flow.
- Add collapsible detail blocks per tool call:
  - command/tool name
  - duration
  - concise output summary
- Keep default view compact; expand on demand.

2. Empty-state and startup experience
- Add a minimal startup splash/banner with quick actions.
- Show first-prompt examples and useful entry points.
- Keep it lightweight and disappear once chat starts.

3. Composer + assist row polish
- Improve bottom assist/hint row with richer context:
  - mode/provider/model
  - context budget hint
  - queued skill / tool usage cues
- Add non-intrusive autocomplete preview behavior.

4. Adaptive layout (reclaim space)
- Auto-hide notification/cron surfaces when inactive.
- Use full-width chat when no alerts/cron widgets are needed.
- Keep instant toggle available for notifications when present.

5. Streaming readability polish
- Improve typography and spacing for long streamed responses.
- Tune chunk rendering rhythm to feel smoother.
- Add subtle status transitions without noisy popups.

## Suggested Validation Checklist

- Run: `uv run ruff check src/agnoclaw/tui tests/test_tui_app_commands.py`
- Run: `uv run --extra dev pytest -q tests/test_tui_app_commands.py`
- Live test: `uv run agnoclaw tui --provider ollama --model qwen3:8b`
- Manual checks:
  - Empty chat shows floating composer behavior
  - `?` shortcuts/hints work inline
  - Tool traces appear inline in chat and are readable
  - Notifications panel only consumes space when actually needed
