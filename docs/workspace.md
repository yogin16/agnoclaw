# Workspace Files Reference

agnoclaw's workspace is a directory of plain Markdown files at `~/.agnoclaw/workspace/` (configurable). All files are human-readable, grep-able, and git-backup friendly.

Inspired by OpenClaw's workspace, with the same file names and loading semantics.

---

## File Reference

### `AGENTS.md` — Behavioral guidelines

The agent's operating contract. Sets rules for how the agent should behave:
- Session startup behavior
- Memory management rules (write to files, never rely on in-session memory)
- Safety principles (no data exfiltration, no destructive actions without consent, no unsolicited external communications)
- Proactive behaviors (what to watch for during heartbeat)

**Default content provided by `agnoclaw init`.**

---

### `SOUL.md` — Personality and ethics

Core character traits and ethical foundation:
- Communication style and tone
- Personal opinions and preferences
- Trust and competence principles
- What the agent genuinely cares about vs performative helpfulness

The file is meant to evolve across sessions as the agent learns what works.

---

### `IDENTITY.md` — Agent identity card

Who the agent is:
- Name
- Creature/type (e.g., "a senior software engineer and technical advisor")
- Vibe and communication style descriptor
- Optional: specializations, areas of expertise

---

### `USER.md` — User profile

What the agent knows about you:
- Name and preferred address
- Timezone
- Communication preferences
- Current projects and goals
- Things that matter to you

Create this file yourself or populate it during `agnoclaw init`.

---

### `TOOLS.md` — Tool configuration policy

Operational notes and restrictions:
- Allowed/disallowed commands
- Preferred package managers (`uv add` not `pip install`)
- Path conventions and host-specific quirks
- Known risky operations and when they're safe
- SSH aliases, service names, and environment specifics

**Not executable config** — just instructions for the agent.

---

### `HEARTBEAT.md` — Periodic task checklist

What to check on each heartbeat tick:
- Items the agent monitors proactively
- Alert conditions ("disk usage > 80%", "unreviewed PRs")
- If empty (or headers-only), heartbeat runs silently with zero cost

Reply `HEARTBEAT_OK` if nothing needs attention.

---

### `BOOT.md` — Startup sequence

Actions to take when the agent starts a new session:
- Load project context
- Check for unresolved tasks
- Send a morning summary
- Any startup ritual

Runs once at session start. Agents reply `NO_REPLY` if no user message needed.

---

### `MEMORY.md` — Curated long-term memory

Persistent facts and compressed session history:
- Important decisions and their rationale
- Key architectural facts about current projects
- User preferences confirmed across multiple sessions
- Learning summaries from previous sessions

**First 200 lines loaded into every session context.**
Keep it concise — one fact per line or short paragraphs.

---

### `memory/YYYY-MM-DD.md` — Daily session logs

Auto-created daily log files. Store:
- Session summaries
- Progress on ongoing tasks
- Context needed for next session

Call `agent.save_session_summary(text)` to write to today's log.

---

## Loading Order

`SystemPromptBuilder` injects workspace files near the end of the system prompt (highest precedence):

```
AGENTS → SOUL → IDENTITY → USER → MEMORY (first 200 lines) → TOOLS → BOOT
```

All files are optional — missing files are silently skipped.

---

## Comparison with OpenClaw

| File | agnoclaw | OpenClaw | Notes |
|---|---|---|---|
| `AGENTS.md` | Yes | Yes | Identical purpose |
| `SOUL.md` | Yes | Yes | Identical purpose |
| `IDENTITY.md` | Yes | Yes | Identical purpose |
| `USER.md` | Yes | Yes | Identical purpose |
| `TOOLS.md` | Yes | Yes | Identical purpose |
| `HEARTBEAT.md` | Yes | Yes | Identical purpose |
| `BOOT.md` | Yes | Yes | Identical purpose |
| `MEMORY.md` | Yes | Yes | First 200 lines loaded |
| `memory/YYYY-MM-DD.md` | Yes | Yes | Daily auto-logs |
| `BOOTSTRAP.md` | No | Yes | Self-destructing onboarding script |
| `SHIELD.md` | No | Community | Security policy file |
| `hooks/` | No | Yes | Event hook definitions |
| `skills/` | Yes | Yes | Workspace skill overrides |

**agnoclaw additions vs OpenClaw:**
- 3-tier memory: workspace files + LearningMachine user stores (SQLite) + LearningMachine institutional stores
- `ProgressToolkit`: multi-context-window feature tracking (`progress.md` + `features.md`)
- `.learnings/` directory via `self-improving-agent` skill

---

## CLI

```bash
# Initialize workspace with defaults
agnoclaw init

# Show current workspace files
agnoclaw workspace show

# Edit a workspace file
$EDITOR ~/.agnoclaw/workspace/SOUL.md
```

---

*See [harness-gap-analysis.md](harness-gap-analysis.md) for the unified Claude Code + OpenClaw gap status.*
