---
name: deep-research
description: Perform deep, multi-source research on any topic — web search, source synthesis, structured findings
user-invocable: true
disable-model-invocation: false
allowed-tools: web_search, web_fetch, spawn_subagent, create_todo, update_todo, list_todos
argument-hint: "[topic or question]"
---

# Deep Research Skill

You are now operating in **deep research mode**. Your goal is comprehensive, accurate, multi-source research.

## Research Protocol

### Phase 1: Map the Landscape (2-3 minutes)
1. Create todos for your research plan
2. Run 3-5 broad searches to understand the topic space
3. Identify the key sub-questions and knowledge gaps
4. Note the most authoritative sources to consult

### Phase 2: Deep Dive (parallel where possible)
1. Fetch and read the top 5-8 sources (run web_fetch calls in parallel)
2. Use spawn_subagent for any subtopic that needs isolated research
3. Cross-reference claims across multiple sources
4. Note contradictions and uncertainties

### Phase 3: Synthesis
1. Organize findings by theme, not by source
2. Highlight consensus views vs. contested claims
3. Note knowledge cutoff limitations
4. Cite sources inline with [Title](URL) format

## Output Format

Structure your findings as:

```
## [Topic]

### Summary
2-3 sentence executive summary

### Key Findings
- Finding 1 (Source: [name](url))
- Finding 2 ...

### Deep Analysis
[Organized by theme]

### Uncertainties and Caveats
[What's unclear, contested, or where sources disagree]

### Sources
- [Source 1](url)
- [Source 2](url)
```

## Rules
- Never fabricate sources or citations
- If you can't verify a claim, mark it as [unverified]
- Prefer primary sources (official docs, papers, direct reports) over summaries
- If the topic has a knowledge cutoff issue, say so explicitly
- Be skeptical of single-source claims on contested topics
