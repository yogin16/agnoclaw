---
name: skillhub
description: Browse, search, and install skills from ClawHub — the community skill registry
user-invocable: true
disable-model-invocation: false
allowed-tools: bash, web_fetch
argument-hint: "[search query or skill name]"
metadata:
  openclaw:
    emoji: "\U0001F50C"
---

# SkillHub — ClawHub Skill Browser

You are helping the user browse, search, and install skills from the ClawHub community registry.

## Commands

Based on the user's request, do ONE of these:

### Search
If the user provides a search query:
1. Use the ClawHub API to search: `GET https://clawhub.ai/api/v1/skills?q=$ARGUMENTS`
2. Display results as a numbered list with name, description, author, and download count
3. Ask which skill they'd like to inspect or install

### Inspect
If the user asks to inspect a specific skill:
1. Fetch full detail: `GET https://clawhub.ai/api/v1/skills/{name}`
2. Show: description, author, version, dependencies, categories, SKILL.md preview
3. Warn about any required binaries or env vars

### Install
If the user asks to install a specific skill:
1. Download the skill: `GET https://clawhub.ai/api/v1/skills/{name}/download`
2. Save to the workspace skills directory (~/.agnoclaw/workspace/skills/{name}/SKILL.md)
3. Verify the skill loads correctly
4. Show available tools and any required dependencies

### Browse Categories
If the user wants to browse by category:
1. Fetch categories: `GET https://clawhub.ai/api/v1/categories`
2. List them with descriptions
3. Ask which category to explore

## Notes

- All ClawHub reads are public (no API key needed)
- Installed skills go to the workspace skills directory (highest priority)
- Skills from ClawHub get "community" trust level — inline shell commands (!`cmd`) are blocked
- Install specs (pip, brew, npm, etc.) require user approval before running
- If the API is unreachable, suggest manual install: clone the skill repo to ~/.agnoclaw/workspace/skills/
