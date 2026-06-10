# SKILL.md Reference

Skills extend agnoclaw with domain-specific instructions. Compatible with the AgentSkills standard, Claude Code skills, and OpenClaw/ClawHub format.

---

## Directory Structure

```
my-skill/
└── SKILL.md    ← frontmatter + instructions
```

Place skills in:
1. `~/.agnoclaw/workspace/skills/` — workspace overrides (highest priority)
2. `~/.agnoclaw/skills/` — user-level skills
3. Bundled skills (shipped with agnoclaw)

---

## Frontmatter Reference

```yaml
---
name: my-skill
description: "What this skill does and when to use it"
user-invocable: true
disable-model-invocation: false
allowed-tools: bash, web_search, web_fetch, read_file
model: claude-sonnet-4-6
context: fork
argument-hint: "[topic or arg]"
homepage: https://example.com/my-skill
metadata:
  openclaw:
    emoji: 🔧
    os: [darwin, linux]
    always: false
    requires:
      bins: [git, gh]
      anyBins: [brew, apt]
      env: [GITHUB_TOKEN]
    install:
      - type: uv
        package: httpx
      - type: brew
        package: gh
        os: [darwin]
      - type: npm
        package: "@octokit/cli"
        os: [linux]
---
```

### Standard fields

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | string | directory name | Skill identifier |
| `description` | string | `""` | Shown in skill list; used for selective injection |
| `user-invocable` | bool | `true` | Can users activate via `/skill name`? |
| `disable-model-invocation` | bool | `false` | Prevent model from self-activating |
| `allowed-tools` | string or list | `[]` | Comma-separated or YAML list of allowed tool names. When set, a run with this skill sees **only** these tools (restored after the run). See [Per-run tool scoping](#per-run-tool-scoping). |
| `tool-schemas` | map | `{}` | Per-tool input-schema specialization: tool name → JSON Schema. During the run, the named tool advertises this schema (restored after). See [Per-run tool scoping](#per-run-tool-scoping). |
| `model` | string | agent default | Model override for this skill (e.g. `"claude-opus-4-6"`) |
| `context` | string | `null` | Set to `"fork"` to run as isolated subagent (parsed, not yet enforced) |
| `argument-hint` | string | `null` | Hint shown in CLI tab completion |
| `homepage` | string | `null` | Documentation URL |

### OpenClaw metadata (`metadata.openclaw`)

| Field | Type | Description |
|---|---|---|
| `emoji` | string | Display emoji |
| `os` | list | Platform filter: `[darwin]`, `[linux]`, `[win32]` |
| `always` | bool | Skip all gate checks (always load) |
| `requires.bins` | list | All binaries must exist on PATH |
| `requires.anyBins` | list | At least one binary must exist |
| `requires.env` | list | All env vars must be set |
| `install` | list | Installer specs (see below) |

**Aliases:** `metadata.openclaw`, `metadata.clawdbot`, `metadata.clawdis` all work.

### Install specs (`metadata.openclaw.install`)

Run automatically before the skill loads if the dependency is missing:

```yaml
install:
  - type: uv
    package: httpx
  - type: pip
    package: requests>=2.31
  - type: brew
    package: gh
    os: [darwin]       # only on macOS
  - type: npm
    package: "@octokit/cli"
    version: "3.0.0"  # optional version
  - type: go
    package: github.com/cli/cli/v2/cmd/gh@latest
```

| Type | Command | Notes |
|---|---|---|
| `uv` | `python -m uv pip install <pkg>` | Recommended for Python packages |
| `pip` | `python -m pip install <pkg>` | Fallback if uv not available |
| `brew` | `brew install <pkg>` | macOS only |
| `npm` | `npm install -g <pkg>` | Node.js packages |
| `go` | `go install <pkg>` | Go binaries |

Install runs only if the package is not already importable (Python) or binary is not on PATH (brew/npm/go).

---

## Per-run tool scoping

A skill can shape the toolset the model sees **for the duration of a single run**,
without persisting any change to the agent. Two declarations drive this:

### `allowed-tools` — restrict the visible toolset

When a run is invoked with a skill that declares `allowed-tools`, the model sees
only those tools for that run; the full toolset is restored afterward. This now
applies to **inline** skill runs (`harness.run(msg, skill="...")`), not just
`context: fork` skills. It also suppresses the harness's own default toolkits
(bash/files/web/subagent), which a consumer otherwise can't strip — useful when a
skill's entire job is to call one tool and you don't want the model wandering to
`write_file` or `spawn_subagent`.

```yaml
allowed-tools: save_artifact
```

A tool name nested in a toolkit is surfaced on its own, so you can expose a single
function without its siblings. An empty/absent `allowed-tools` means "no
restriction" (consistent with the fork path).

### `tool-schemas` — specialize a tool's input schema

A generic "save"-style tool often exposes an untyped `content: dict`, leaving the
model to guess field names. `tool-schemas` lets the skill advertise the exact
typed shape for that run:

```yaml
allowed-tools: save_artifact
tool-schemas:
  save_artifact:
    type: object
    properties:
      content:
        type: object
        properties:
          company_id: { type: string }
          new_money:  { type: number }
          pre_money:  { type: number }
        required: [company_id, new_money, pre_money]
    required: [content]
```

During the run the model sees the specialized schema; the original is restored
after. You can also pass schemas programmatically per call:

```python
harness.run(msg, skill="saver", tool_schema_overrides={"save_artifact": {...}})
```

Notes:

- This shapes the model's **tool-call input**. It is distinct from `output_schema`,
  which parses the model's **final text response**.
- The harness still recomputes the *top-level* `required` from the tool's
  signature and forces `additionalProperties: false` (an Agno behavior). Overrides
  reliably control nested shapes, types, and descriptions.
- Scoping mutates the live agent for one run, so a single harness instance should
  not have two scoped runs in flight at once (same constraint as the per-run
  system-prompt swap).

---

## Security

Skills can contain executable content (`!`cmd``) and declare package installs. agnoclaw applies a trust-based security model to mitigate supply chain and command injection risks.

### Trust levels

| Level | Source | Inline exec (`!`cmd``) | Install specs |
|---|---|---|---|
| **builtin** | Shipped with agnoclaw | Allowed | Auto-approved |
| **local** | `~/.agnoclaw/workspace/skills/` or `~/.agnoclaw/skills/` | Allowed | User approval required |
| **community** | External (ClawHub, git clone, etc.) | **Blocked** | User approval + validation |

### Package name validation

Before any install runs, package names are checked for:
- Shell metacharacters (`;`, `|`, `&`, `` ` ``, `$`, etc.)
- URL-based installs (`https://`, `git+`, etc.) — blocked to prevent arbitrary code download
- Path traversal (`..`)
- Excessively long names (>200 chars)

Invalid packages are logged and skipped.

### Install approval flow

For non-builtin skills, the user sees what will be installed and must confirm:

```
Skill 'my-skill' requires the following installations:
  uv: httpx
  brew: gh

Proceed with installation? [y/N]
```

To auto-approve in non-interactive contexts (CI, tests):
```python
registry = SkillRegistry(skills_dir, auto_approve_installs=True)
```

### Best practices

- **Review community skills** before installing — check SKILL.md for `!`cmd`` and `install:` specs
- **Prefer builtin/local skills** for sensitive environments
- **Use `agnoclaw skill inspect <name>`** to view a skill's full content before activation
- **Keep skills in version control** for auditability

---

## Content Syntax

### Argument substitution

```markdown
Research this topic: $ARGUMENTS

First arg: $ARGUMENTS[0]
Second arg: $ARGUMENTS[1]
```

### Dynamic context injection

Commands run at render time and their output is spliced in:

```markdown
Current git status:
!`git status --short`

Recent commits:
!`git log --oneline -5`
```

---

## Built-in Skills

| Skill | Description |
|---|---|
| `deep-research` | Multi-source research with structured findings |
| `code-review` | P0/P1/P2/P3 priority code review |
| `git-workflow` | Safe git operations with guardrails |
| `daily-standup` | Generate standup from git history and todos |
| `memory-manage` | Read/update/summarize long-term memory |
| `self-improving-agent` | Capture corrections/errors/feature-requests in `.learnings/`; promote stable patterns to workspace files |

---

## Example: Writing a Skill

Create `~/.agnoclaw/workspace/skills/my-skill/SKILL.md`:

```markdown
---
name: my-skill
description: Analyzes Python files for common anti-patterns
user-invocable: true
allowed-tools: read_file, bash
argument-hint: "[file or dir]"
---

# My Skill

Analyze $ARGUMENTS for Python anti-patterns.

Focus on:
1. Mutable default arguments
2. Bare `except:` clauses
3. Global state

Current directory: !`pwd`
```

Then use it:

```bash
agnoclaw run "analyze src/" --skill my-skill
```

---

## Skill Precedence

When the same skill name exists in multiple locations, the highest-priority location wins:

1. `~/.agnoclaw/workspace/skills/` (workspace overrides)
2. `~/.agnoclaw/skills/` (user-level)
3. Bundled skills (shipped with agnoclaw)

---

## CLI

```bash
# List all available skills
agnoclaw skill list

# Inspect a skill's frontmatter and content
agnoclaw skill inspect deep-research

# Install a skill from a local directory
agnoclaw skill install path/to/my-skill/
```

---

*See [harness-gap-analysis.md](harness-gap-analysis.md) for the unified Claude Code + OpenClaw gap status.*
