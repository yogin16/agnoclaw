# PE Risk Platform Simulation (Embedded Library)

This package simulates private equity risk workflows using `agnoclaw` as an embedded Python library, not a CLI-only demo.

It provides:
- realistic multi-deal fixture packs (`deal_pack_v1` schema)
- deterministic scoring engine with explicit IC-grade criteria
- skill templates for due diligence lifecycle and deal scoring
- end-to-end simulation runner with contract mode and optional live model mode
- PPTX pitch deck ingestion path for real deal onboarding
- multi-attachment ingestion with strict quality gates
- backend service wrapper for chat-style `message + attachments` reviews

## Folder Layout

- `fixtures/deals/*.json`: simulated deal packs (realistic PE-style cases)
- `rubrics/pe_deal_risk_rubric_v1.json`: scoring definitions, weights, action bands
- `skills_templates/*.template.md`: reusable skill templates
- `scoring_engine.py`: deterministic scorer + CLI
- `deal_pack_builder.py`: normalize JSON/PPTX into `deal_pack_v1`
- `simulate_embedding.py`: embedded-library simulation runner
- `quality_gates.py`: schema and completeness gates
- `service.py`: embed-ready review orchestrator

## Canonical Schema (`deal_pack_v1`)

Required top-level keys:
- `deal_id`, `deal_name`, `sponsor`, `strategy`, `sector`
- `transaction.debt_package` with leverage + covenant fields
- `metrics` (quantitative risk signals)
- `diligence_flags` (severity + dimension)
- `open_diligence_questions`

Optional keys:
- `expected_outcome` for simulated fixtures (used by contract validation)
- `source_material` for ingestion traceability (e.g., PPTX extraction)

Full schema reference: `examples/pe_risk_platform/deal_pack_schema.md`

## Run Deterministic Scoring

Single deal:

```bash
uv run python examples/pe_risk_platform/scoring_engine.py score \
  examples/pe_risk_platform/fixtures/deals/PE-2026-001-northstar-vertical-software.json
```

Batch:

```bash
uv run python examples/pe_risk_platform/scoring_engine.py batch \
  examples/pe_risk_platform/fixtures/deals --fail-on-mismatch
```

## Run Embedded Simulation

Contract mode (no model call; validates skills + scoring + expected outcomes):

```bash
uv run python examples/pe_risk_platform/simulate_embedding.py \
  --mode contract --use-fixtures --format text --fail-on-errors
```

Live mode (real model calls through `AgentHarness`):

```bash
export ANTHROPIC_API_KEY=your_key
uv run python examples/pe_risk_platform/simulate_embedding.py \
  --mode live --model anthropic:claude-sonnet-4-6 --format text
```

## Onboard Real Deals (JSON or PPTX)

From existing JSON:

```bash
uv run python examples/pe_risk_platform/deal_pack_builder.py from-json \
  --input /path/to/raw_deal.json --output /tmp/PE-REAL-001.json
```

From pitch deck PPTX:

```bash
uv run python examples/pe_risk_platform/deal_pack_builder.py from-pptx \
  --input /path/to/pitch_deck.pptx \
  --deal-id PE-REAL-002 \
  --deal-name "Project Atlas" \
  --sector "Healthcare Services" \
  --output /tmp/PE-REAL-002.json
```

From multiple attachments (deck + QoE memo + financial extract):

```bash
uv run python examples/pe_risk_platform/deal_pack_builder.py from-attachments \
  --input /path/to/pitch_deck.pptx \
  --input /path/to/qoe_notes.docx \
  --input /path/to/financials.xlsx \
  --deal-id PE-REAL-003 \
  --deal-name "Project Orion" \
  --sector "Industrials" \
  --output /tmp/PE-REAL-003.json
```

Then run simulation on that deal file:

```bash
uv run python examples/pe_risk_platform/simulate_embedding.py \
  --mode contract --deal-file /tmp/PE-REAL-002.json
```

## Notes for Real Deployments

- Keep `deal_pack_v1` as the stable integration contract between data ingestion, scoring, and skill execution.
- Treat rubric versions as explicit (`v1`, `v1.1`, etc.) to preserve historical IC decision auditability.
- Use `--save-report` in `simulate_embedding.py` to persist machine-readable run artifacts for QA and model regression tracking.
- Default to strict quality gates; avoid scoring on partial/inferred metrics unless explicitly in exploratory mode.

## Interactive Chat Harness (With Events)

Run interactive PE chat:

```bash
uv run python examples/pe_risk_platform/harness_chat.py \
  --model anthropic:claude-sonnet-4-6
```

Show full intermediate harness events in terminal:

```bash
uv run python examples/pe_risk_platform/harness_chat.py \
  --model anthropic:claude-sonnet-4-6 \
  --show-events
```

Persist all events as NDJSON (for audit/regression analysis):

```bash
uv run python examples/pe_risk_platform/harness_chat.py \
  --model anthropic:claude-sonnet-4-6 \
  --show-events \
  --events-file /tmp/pe-risk-events.ndjson
```

## Backend Usage (Chat-style)

```python
from examples.pe_risk_platform.service import PERiskReviewService

service = PERiskReviewService(
    model="anthropic:claude-sonnet-4-6",
    workspace_dir="/srv/pe-risk/workspace",
)

result = service.review(
    user_message="Review this deal and give IC-ready downside risk recommendation.",
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
