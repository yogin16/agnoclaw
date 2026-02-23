---
name: daily-standup
description: Generate a daily standup report from git history, todos, and workspace memory
user-invocable: true
disable-model-invocation: false
allowed-tools: bash, read_file, list_todos
argument-hint: "[date: today|yesterday|YYYY-MM-DD]"
---

# Daily Standup Skill

Generate a concise daily standup report. Gather facts first, synthesize second.

## Data Collection

Run these in parallel:

1. **Git activity** (last 24h):
   ```bash
   git log --since="24 hours ago" --oneline --all --author="$(git config user.name)"
   ```

2. **Uncommitted work**:
   ```bash
   git status --short
   git diff --stat
   ```

3. **Recent todos**: call `list_todos` to see pending and completed items

4. **Memory** (if MEMORY.md exists): read it for context on ongoing work

## Output Format

```
## Standup — [Date]

### Yesterday / Done
- [Specific completed items from git log and completed todos]

### Today / In Progress
- [Active todos + uncommitted work in progress]

### Blockers
- [Anything blocking progress, or "None"]

### Notes
- [Optional: anything worth flagging to the team]
```

## Rules
- Be specific — list actual commit messages and task names, not vague summaries
- If there's nothing to report for a section, say "None" (don't omit the section)
- Keep each bullet to one line
- Do NOT include times or token counts unless asked
