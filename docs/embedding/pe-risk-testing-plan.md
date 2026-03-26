# PE Risk Management: Embedded Library Test Plan

This plan validates `agnoclaw` embedded as a Python library inside a separate backend platform for private equity risk workflows.

## Scope

- Library-first usage (`AgentHarness` + `ExecutionContext`), not CLI behavior.
- Two custom skills:
  - `pe-due-diligence-lifecycle` (reasoning-only, `SKILL.md` only)
  - `pe-deal-scoring` (reasoning + deterministic computation via local script)
- Realistic simulated deal context using canonical `deal_pack_v1` schema.
- End-to-end checks across skill loading, policy, events, and run execution.

## Test Layers

1. Simulation contract layer (deterministic, no API key):
- Skill parse/render behavior for both PE skills.
- Inline command execution trust model:
  - local skill: execute ``!`cmd` ``
  - community skill: preserve ``!`cmd` `` (blocked execution)
- Policy denial on `before_skill_load` for missing role.
- Event emission and context propagation for embedded runs.
- Deal-pack and rubric consistency checks across realistic fixtures.

2. Live integration (optional):
- Run the same PE skill flows against a real model (Ollama or Anthropic).
- Validate response non-empty + expected structure (risk register / IC action).

## What Is Implemented

- Simulation package: `examples/pe_risk_platform/`
  - realistic fixtures: `fixtures/deals/*.json`
  - scoring rubric: `rubrics/pe_deal_risk_rubric_v1.json`
  - deterministic scorer: `scoring_engine.py`
  - real-deal onboarding builder (JSON/PPTX): `deal_pack_builder.py`
  - multi-attachment onboarding (PPTX/DOCX/XLSX/PDF/JSON): `deal_pack_builder.py from-attachments`
  - schema/completeness gates: `quality_gates.py`
  - embedded runner: `simulate_embedding.py`
  - backend wrapper (`message + attachments`): `service.py`
- Test module: `tests/test_pe_embedding_library.py`
- Test module: `tests/test_pe_risk_platform.py`
- Scenarios covered:
  - reasoning-only skill load
  - reasoning+computation skill load (local trust executes script)
  - community trust blocks inline execution
  - `AgentHarness.run(..., skill=...)` E2E with `ExecutionContext` + event sink
  - role-based policy denial for restricted skill
  - fixture score/band/action conformance to rubric
  - PPTX metric extraction smoke path for pitch deck onboarding

## Runbook

Run deterministic scoring on fixture portfolio:

```bash
uv run python examples/pe_risk_platform/scoring_engine.py batch \
  examples/pe_risk_platform/fixtures/deals --fail-on-mismatch
```

Run full embedded simulation (contract mode, no model call):

```bash
uv run python examples/pe_risk_platform/simulate_embedding.py \
  --mode contract --use-fixtures --fail-on-errors
```

Build a real-deal pack from PPTX:

```bash
uv run python examples/pe_risk_platform/deal_pack_builder.py from-pptx \
  --input /path/to/pitch_deck.pptx \
  --deal-id PE-REAL-001 \
  --deal-name "Project Atlas" \
  --sector "Healthcare Services" \
  --output /tmp/PE-REAL-001.json
```

Build from multiple attachments:

```bash
uv run python examples/pe_risk_platform/deal_pack_builder.py from-attachments \
  --input /path/to/pitch_deck.pptx \
  --input /path/to/qoe_notes.docx \
  --input /path/to/financials.xlsx \
  --deal-id PE-REAL-002 \
  --deal-name "Project Orion" \
  --sector "Industrials" \
  --output /tmp/PE-REAL-002.json
```

Run embedded simulation on one real deal pack:

```bash
uv run python examples/pe_risk_platform/simulate_embedding.py \
  --mode contract --deal-file /tmp/PE-REAL-001.json --fail-on-errors
```

Use backend service for chat-style `message + attachments`:

```python
from examples.pe_risk_platform.service import PERiskReviewService

svc = PERiskReviewService(
    model="anthropic:claude-sonnet-4-6",
    workspace_dir="/srv/pe/workspace",
)

result = svc.review(
    user_message="Review this deal and provide IC-ready recommendation.",
    attachments=[
        "/data/deals/project_atlas/pitch_deck.pptx",
        "/data/deals/project_atlas/qoe_memo.docx",
        "/data/deals/project_atlas/monthly_financials.xlsx",
    ],
    deal_id="PE-REAL-ATLAS-001",
    deal_name="Project Atlas",
    sector="Healthcare Services",
)
```

Run PE library tests:

```bash
uv run --extra dev pytest tests/test_pe_embedding_library.py tests/test_pe_risk_platform.py -v
```

Run full integration suite (skips if provider unavailable):

```bash
uv run --extra dev pytest tests/test_integration.py -m integration -v
```

Run integration with Anthropic explicitly:

```bash
export AGNOCLAW_TEST_PROVIDER=anthropic
export AGNOCLAW_TEST_MODEL=claude-haiku-4-5-20251001
export ANTHROPIC_API_KEY=your_key_here
uv run --extra dev pytest tests/test_integration.py -m integration -v
```

## Pass/Fail Gates

- Deterministic fixture scoring passes expected ranges, bands, and IC actions.
- Embedded contract simulation reports zero failures on fixture set.
- `skill.load.started` and `skill.load.completed` events emitted when skill is requested.
- Computation skill produces deterministic score output in local trust mode.
- Same computation skill does not execute inline commands in community trust mode.
- Policy-denied runs fail with `HarnessError(code="POLICY_DENIED")` and do not call model runtime.
- Real-deal onboarding path works for JSON and PPTX source materials into the same `deal_pack_v1` schema.
