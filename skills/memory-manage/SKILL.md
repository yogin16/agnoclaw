---
name: memory-manage
description: Read, update, organize, and summarize the agent's long-term memory (MEMORY.md and daily logs)
user-invocable: true
disable-model-invocation: false
allowed-tools: read_file, write_file, edit_file, glob_files
argument-hint: "[action: show|add|summarize|clean]"
---

# Memory Management Skill

Manage the agent's long-term memory stored in the workspace.

## Memory File Structure

```
~/.agnoclaw/workspace/
├── MEMORY.md          ← curated long-term memory (cross-session)
├── memory/
│   ├── 2026-01-15.md  ← daily log for Jan 15
│   ├── 2026-01-16.md  ← daily log for Jan 16
│   └── ...
```

## Actions

### show
Read and display current MEMORY.md contents. Also list available daily logs.

### add [content]
Append a new memory entry to MEMORY.md.
Format:
```
## [YYYY-MM-DD] [Category]
[Content]
```

### summarize
Read the last 7 daily logs and update MEMORY.md with a weekly summary.
- Extract recurring themes, decisions, and preferences
- Prune outdated or superseded entries from MEMORY.md
- Keep MEMORY.md under 200 lines (the auto-load limit)

### clean
Review MEMORY.md for:
- Duplicate or contradictory entries → resolve
- Outdated entries (projects no longer relevant) → archive or remove
- Entries that should be in SOUL.md or USER.md instead → move them

## Memory Content Guidelines

Good memory entries:
- User preferences: "Prefers Python over JavaScript for tooling"
- Project conventions: "This repo uses pytest fixtures in conftest.py"
- Recurring patterns: "User always wants test coverage before merging"
- Decisions: "Decided to use PostgreSQL (not SQLite) for production in Project X"

Bad memory entries (don't persist these):
- Session-specific context ("currently working on feature Y")
- Information that can be found in the codebase
- Speculation ("might want to...")
- Anything that belongs in a config file instead

## Rules
- Keep MEMORY.md under 200 lines — it's loaded in full on every session
- Prefer updating existing entries over adding duplicates
- Date all entries for freshness tracking
- If memory contradicts itself, resolve by preferring the more recent entry
