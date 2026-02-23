---
name: self-improving-agent
description: Record corrections, errors, and feature requests in a structured learnings log; periodically promote durable patterns to workspace files
user-invocable: true
disable-model-invocation: false
allowed-tools: read_file, write_file, edit_file, glob_files, bash
argument-hint: "[record|review|promote|status]"
---

# Self-Improving Agent Skill

This skill implements structured self-learning: capturing corrections, errors, and feature
requests in `.learnings/`, then promoting stable patterns to workspace files.

Inspired by OpenClaw's community `self-improving-agent` and `agent-reflect` skills.

---

## When to Activate This Skill

Activate automatically when any of these events occur:

- A shell command fails unexpectedly (non-zero exit code you didn't anticipate)
- The user explicitly corrects you: "No, that's wrong", "never do X", "stop doing Y"
- The user requests a capability you cannot fulfill
- An external API or tool call fails and you discover a workaround
- You discover a better approach to something you've done before
- You catch yourself about to repeat a mistake you've seen before
- At the end of a long session before context compaction

Also activate on explicit commands:
- `record` / `learn` — record a new learning now
- `review` — review all pending learnings
- `promote` — promote stable learnings to workspace files
- `status` — show learning counts and recent entries

---

## File Structure

All files live under `.learnings/` in the workspace root (not the project directory):

```
.learnings/
├── LEARNINGS.md       ← corrections, patterns, best practices
├── ERRORS.md          ← command failures, stack traces, workarounds
└── FEATURE_REQUESTS.md ← capabilities requested but not yet available
```

These files are gitignored by convention (they are per-session institutional memory, not code).

---

## Entry Format

Each entry follows this structure:

```markdown
### [TYPE]-[YYYYMMDD]-[NNN]: [Short title]

- **id**: LRN-20260223-001
- **timestamp**: 2026-02-23T14:30:00Z
- **priority**: high | medium | low
- **status**: pending | promoted | wont_fix
- **area**: tools | workflow | domain | security | performance | ux
- **source**: [what triggered this — user correction / command failure / observation]
- **related_files**: [comma-separated paths if applicable]

**Summary**: One sentence describing what was learned.

**Details**: Full context. What happened, what went wrong, what the better approach is.

**Suggested action**: What should change — update AGENTS.md, add to TOOLS.md, etc.
```

Entry ID types:
- `LRN-` — general learning (correction, pattern, best practice)
- `ERR-` — error/failure with workaround
- `FEAT-` — feature request or capability gap

---

## Recording a Learning

When an event triggers self-learning:

1. Determine the entry type (LRN / ERR / FEAT)
2. Read `.learnings/LEARNINGS.md` (or ERRORS.md / FEATURE_REQUESTS.md) to find the next sequence number
3. Write the new entry in the appropriate file
4. Briefly acknowledge: "Recorded as LRN-YYYYMMDD-NNN."

Do NOT write learnings for:
- Ephemeral session state (what we did in this task)
- Things already in workspace files
- Obvious facts that won't generalize

---

## Reviewing Learnings

When asked to `review`, or when more than 20 pending entries exist:

1. Read all three `.learnings/` files
2. For each pending entry, assess:
   - Is this still relevant?
   - Is this pattern now stable / confirmed multiple times?
   - Should it be promoted, closed as wont_fix, or left pending?
3. Summarize counts: "10 pending, 3 ready to promote, 2 wont_fix"

---

## Promoting Learnings to Workspace Files

Promote a learning when:
- The same pattern has been observed 2+ times, OR
- The user asks for promotion, OR
- A learning is marked high priority and clearly generalizable

**Promotion targets** (choose the most appropriate):

| Learning type | Target file |
|---|---|
| Behavioral rules ("always X", "never Y") | `AGENTS.md` |
| Tool usage patterns | `TOOLS.md` |
| Identity / persona adjustments | `SOUL.md` |
| Capability/knowledge updates | `IDENTITY.md` |

**Promotion process:**

1. Read the target workspace file
2. Append the learning as a bullet under the appropriate section
3. Update the entry's status to `promoted` with the target file noted
4. Confirm: "Promoted LRN-YYYYMMDD-NNN → AGENTS.md"

**Never promote:**
- Error stack traces (keep in ERRORS.md only)
- Feature requests (keep in FEATURE_REQUESTS.md until implemented)
- Anything user-specific (goes to USER.md via standard workspace update, not promotion)

---

## Pre-Compaction Flush

Before a session ends or context compacts:

1. Check if there are any high-value learnings not yet recorded
2. Write them now — context loss is worse than a slightly premature record
3. Briefly note: "Session learnings flushed to .learnings/"

---

## Initialization

If `.learnings/` does not exist, create the directory and stub files:

```markdown
# Learnings

Corrections, patterns, and best practices discovered through experience.
See ERRORS.md for command failures, FEATURE_REQUESTS.md for capability gaps.
```

```markdown
# Errors

Command failures, unexpected tool behavior, and their workarounds.
```

```markdown
# Feature Requests

Capabilities requested by users but not yet available.
```

---

## Rules

- Record immediately — don't defer until "later" (context compaction will erase it)
- Be specific: vague learnings have no value
- One learning per entry — don't bundle multiple insights
- Don't over-record: frequency noise degrades signal
- The test: "Would this help a fresh agent avoid a mistake?"
