# Provider-free public API journey

Status: executable source-side clean-room foundation; exact-wheel/network-denied gate
remains release-blocking

This probe exercises the copyable public grammar before packaging. It imports only
top-level `agnoclaw` exports, constructs no provider SDK client, writes only to a fresh
temporary directory, and emits one content-free JSON record:

```bash
uv run python scripts/public_api_journey_probe.py
```

To retain local evidence for inspection, provide an existing empty directory. The
probe refuses any nonempty target rather than deleting or overwriting it:

```bash
mkdir /tmp/agnoclaw-public-journey
uv run python scripts/public_api_journey_probe.py \
  --root /tmp/agnoclaw-public-journey
```

## What it executes

| Journey | Public contract exercised | Required truth |
|---|---|---|
| short run | `AgnoModelFactory` + `HarnessConfig.quick()` + `arun()` | one deterministic model call, terminal result, base/run transport cleanup |
| durable continuation | `HarnessConfig.durable()` + `start()` + `HarnessSession.start()` | two logical runs, same trusted session, distinct run-owned models |
| reopen | `get_run()` over reopened SQLite runtime/Agno stores | both terminal run states survive complete harness/store reconstruction |
| governed learning | capture → held-out evaluation → operator promotion | one qualified candidate, one reversible promotion effect, exact promoted state after reopen |
| local migration | plan → apply → verify → cutover → rollback | personal row rekeyed, all four phases verified, target removed by exact rollback |

The deterministic model returns a fixed value, but response, learning content, artifact
addresses, user data, and filesystem paths never enter stdout. A successful record has
this stable shape (version and elapsed time vary):

```json
{"cleanup":"complete","durable_and_learning":{"learning_effects":1,"learning_state":"promoted","logical_runs":2,"model_invocations":2,"owned_models_closed":5,"profile":"durable","reopened_completed_runs":2},"migration":{"personal_rows":1,"phases":["applied","verified","cutover","rolled_back"],"rollback_removed_target":true},"network_boundary":"provider_free; OS network denial is a separate installed-wheel gate","production_certification":false,"provider_network_calls":0,"quick":{"model_invocations":1,"owned_models_closed":2,"profile":"quick","terminal":true},"schema_version":"1.0"}
```

## What it does not prove

This source-side probe is not the final installed demonstration. It does not build a
wheel, deny networking at the operating-system/container boundary, kill a process,
exercise PostgreSQL/service deployment, contact a live provider, benchmark cold
installation, or certify production recovery. Those remain separate gates so a
provider-free model is never mislabeled as an air-gapped environment and source imports
are never mislabeled as installed-artifact proof.

Before release, this exact file must run unchanged outside the repository against the
exact candidate wheel. T16 must additionally run the real-process restart demonstration
with OS-level network denial and record wall time. Packaging stays last; developing and
testing the journey now prevents package iterations from hiding API/documentation bugs.

## Current retained evidence

On 2026-08-18 the source-side probe completed all five rows in 2.232 seconds on Python
3.13 / Agno 2.9.0 / agnoclaw 0.11.0 development metadata, with three model invocations,
seven owned model closes, two reopened completed runs, one promoted learning effect, exact
migration rollback, zero provider network calls, and complete temporary cleanup. The
corresponding tests also reject private imports, response/learning/user content on
stdout, and nonempty operator roots.
