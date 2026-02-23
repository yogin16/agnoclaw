---
name: git-workflow
description: Git operations — commit, branch, PR creation, conflict resolution — with safety guardrails
user-invocable: true
disable-model-invocation: false
allowed-tools: bash, read_file, glob_files
argument-hint: "[operation: commit|branch|pr|status|log]"
---

# Git Workflow Skill

You are now in **git workflow mode**. Execute git operations safely and predictably.

## Safety Rules (non-negotiable)

- **NEVER** force-push to main/master — refuse and explain why
- **NEVER** `git reset --hard` without explicit user confirmation of what will be lost
- **NEVER** skip hooks (`--no-verify`) unless the user explicitly requests it
- **NEVER** `git add -A` or `git add .` — stage specific files by name
- **NEVER** commit without being asked to
- Always check `git status` before any write operation
- If there's unexpected state (unfamiliar files, branches), investigate before acting

## Standard Workflows

### Commit
1. `git status` — understand what's changed
2. `git diff [specific files]` — review changes
3. `git log --oneline -5` — understand commit style
4. Stage specific files: `git add path/to/file`
5. Commit with descriptive message (see format below)
6. `git status` — verify clean state

### Commit Message Format
```
[type]: [short description under 72 chars]

[optional body: what and why, not how]

Co-Authored-By: agnoclaw <noreply@agnoclaw.ai>
```
Types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `perf`

### Branch
1. `git status` — confirm clean working tree
2. `git fetch origin` — get latest remote state
3. `git checkout -b [branch-name] [base-branch]`
4. Branch naming: `feat/description`, `fix/issue-123`, `chore/update-deps`

### Pull Request
1. `git log origin/main..HEAD --oneline` — see what's in the PR
2. `git diff origin/main..HEAD --stat` — see changed files
3. Use `gh pr create` with title and body
4. PR title: under 70 chars, describes the change (not the ticket number)

### Conflict Resolution
1. `git status` — identify conflicted files
2. Read each conflicted file — understand both sides before resolving
3. Resolve by understanding intent, not by blindly taking one side
4. `git add [resolved files]`
5. `git rebase --continue` or `git merge --continue`

## Output Format

After any git operation, confirm:
```
[operation] done.
Branch: [current branch]
Status: [clean|N files changed]
Last commit: [hash] [message]
```
