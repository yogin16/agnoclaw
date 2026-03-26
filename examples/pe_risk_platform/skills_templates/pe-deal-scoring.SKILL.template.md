---
name: pe-deal-scoring
description: Deterministic PE deal downside-risk scoring rubric with explicit IC action mapping
allowed-tools: bash, files
---

# PE Deal Scoring Skill

Run the deterministic scoring rubric and use it as the primary quantitative anchor.

## Score Snapshot
!`__SCORING_COMMAND__`

## Risk Band and Action Definitions
- LOW (0-39): Proceed
- MEDIUM (40-64): Proceed with conditions
- HIGH (65-79): Pause / reprice
- SEVERE (80-100): No-go until mitigated

## Required output sections
1. Composite Risk Score
2. Top Score Drivers
3. Investment Committee Action

Do not ignore qualitative diligence flags. If qualitative severity contradicts score optimism, call out escalation explicitly.
