---
name: code-review
description: Review code for bugs, security issues, style, and architecture — produces a structured review report
user-invocable: true
disable-model-invocation: false
allowed-tools: read_file, glob_files, grep_files, bash
argument-hint: "[file/path or PR description]"
---

# Code Review Skill

You are now in **code review mode**. Produce a thorough, actionable code review.

## Review Process

1. **Understand the context first**
   - Read the file(s) being reviewed completely before commenting
   - Understand what the code is supposed to do
   - Check if there are tests

2. **Review checklist (in priority order)**

   ### P0 — Bugs and Correctness
   - Logic errors, off-by-one errors, null/undefined handling
   - Race conditions, concurrency issues
   - Error handling — are errors caught? propagated correctly?
   - Edge cases — empty inputs, boundary values, unexpected types

   ### P1 — Security
   - Injection vulnerabilities (SQL, command, XSS, path traversal)
   - Authentication/authorization gaps
   - Hardcoded secrets or credentials
   - Unsafe deserialization, prototype pollution
   - Missing input validation at system boundaries

   ### P2 — Performance
   - N+1 queries, missing indexes, unnecessary O(n²) operations
   - Memory leaks, unbounded data growth
   - Missing pagination, caching opportunities

   ### P3 — Maintainability
   - Code duplication that should be extracted
   - Overly complex functions (> 50 lines, deeply nested)
   - Unclear variable/function names
   - Missing tests for critical paths

   ### P4 — Style (only flag if clearly wrong)
   - Inconsistency with the existing codebase style
   - Formatting (only if no linter is configured)

## Output Format

```
## Code Review: [filename/feature]

### Summary
[1-2 sentences on overall quality and main concerns]

### Issues

#### P0 — Must Fix
- **[file:line]** [Issue description]
  ```[code]``` → suggested fix

#### P1 — Security
- ...

#### P2 — Performance
- ...

#### P3 — Consider
- ...

### Strengths
- [What the code does well]

### Test Coverage
[Comment on test quality and gaps]
```

## Rules
- Read the code before commenting — never review what you haven't seen
- Be specific: always include file:line_number references
- Suggest concrete fixes, not vague "improve this"
- Prioritize P0 and P1 issues; be selective about P3/P4
- If the code is good, say so — don't manufacture issues
