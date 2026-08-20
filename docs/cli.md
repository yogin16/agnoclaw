# CLI reference

The CLI is an optional adapter over the embedded harness. Install it with:

```bash
pip install "agnoclaw[cli]"
```

Use `agnoclaw COMMAND --help` as the installed-version source of truth.

## Agent commands

```bash
agnoclaw init
agnoclaw chat
agnoclaw chat --sync
agnoclaw run "Summarize the repository"
agnoclaw tui
```

`chat` uses the async REPL by default. `--sync` selects the legacy blocking adapter.
`tui` additionally requires `agnoclaw[tui]`.

Both `chat` and `run` accept:

| Option | Meaning |
|---|---|
| `--model`, `-m` | Model ID |
| `--provider`, `-p` | Provider name |
| `--session`, `-s` | Persistent session ID |
| `--workspace`, `-w` | Workspace directory |
| `--debug` | Show debug/tool-call output |
| `--permission-mode` | `bypass`, `default`, `accept_edits`, `plan`, or `dont_ask` |

`run` also accepts `--skill SKILL_NAME`.

Examples:

```bash
agnoclaw run "Review src/" --skill code-review --permission-mode plan
agnoclaw run "Summarize README.md" --provider ollama --model qwen3:8b
agnoclaw chat --session incident-42 --permission-mode default
```

### Execution route by runtime profile

The selected profile comes from normal configuration, including
`AGNOCLAW_PROFILE`. The first-party adapters currently route work as follows:

| Client path | explicit `quick` / `durable` / `service` | named `legacy` |
|---|---|---|
| `agnoclaw run` | `AgentHarness.start()` plus `HarnessRun.wait()`; final output is printed after settlement | Human-facing provider token stream through the direct compatibility call |
| async `agnoclaw chat` and `tui` messages | Lifecycle-governed model operation plus a bounded live presentation stream; the client waits for the authoritative terminal result | Raw provider stream |
| `agnoclaw chat --sync` | Lifecycle-governed raw display through the reusable harness-owned sync coordinator; close drains/cancels lifecycle work and joins the coordinator | Raw blocking provider stream |
| heartbeat and JSON compatibility schedule execution | Lifecycle start/wait with a logical run ID | Non-streaming direct compatibility call |
| `schedule worker --runtime-db` | Database-clock occurrence claims plus lifecycle start/wait, lease renewal, and reattachment | not applicable |

The explicit-profile live presentation is a process-local display attachment, not a
second result or event authority. If a TUI/terminal consumer is slow or disconnects,
the bounded display detaches instead of blocking or cancelling the run. In parallel,
extracted provider text is committed in scoped, bounded artifacts and can be replayed
with `HarnessRun.output()`; authenticated remote lifecycle starts expose the same
cursor-paged stream. `HarnessRun.events()` remains the content-minimized lifecycle
stream, and the terminal result remains authoritative.

Every CLI-owned harness is closed on success, failure, and normal exit. Async commands
close it on the event loop that created its resources. Ctrl+C for an embedded durable
one-shot or interactive worker uses explicit cancellation; merely detaching a live
display does not. An in-process worker is not claimed to survive its process. Use
AgentOS/remote lifecycle routes or an owned long-lived service loop when work must
outlive the initiating client.

## Workspace and skills

```bash
agnoclaw workspace init
agnoclaw workspace show

agnoclaw skill list
agnoclaw skill inspect code-review
agnoclaw skill install ./my-skill

agnoclaw hub search "incident response"
agnoclaw hub inspect SKILL_NAME
agnoclaw hub install SKILL_NAME
```

Community skills are untrusted input. Inspect their content, tool scope, dynamic command
injection, and install specifications before approval. The runtime trust contract is in
[SKILL.md reference](skills.md).

Hub downloads are placed under a `.community/` quarantine, not in a locally trusted
skill root. Installation applies bounded archive validation and records source/digest
provenance. `skill list` reports the effective trust level, while `skill inspect` and
Hub install verification are parse-only: they do not execute `!` commands, render
dynamic content, or run installers. Explicit activation of a community skill still
leaves inline command syntax literal. By contrast, `skill install ./local-directory`
is a deliberate local installation and receives local trust; review the directory
before invoking it.

## Packs

```bash
agnoclaw pack list
agnoclaw pack inspect ./my-pack
agnoclaw pack install ./my-pack
agnoclaw pack trust PACK_NAME
agnoclaw pack remove PACK_NAME
```

Inspection does not execute pack code. Trust is an explicit host step for any pack with
Python registrations. The trust record lives outside the pack, binds its canonical
installed identity to the exact pack digest, and is invalidated by any content change;
an in-pack marker cannot grant trust. Untrusted pack skills always register as
community content. Programmatic `trusted_packs=True` is a host assertion, not a
manifest-controlled shortcut.

## Heartbeat

```bash
agnoclaw heartbeat trigger
agnoclaw heartbeat start --interval 30
agnoclaw heartbeat install-service --interval 30
agnoclaw heartbeat install-service --uninstall
```

The service installer targets launchd on macOS and a systemd user service on Linux.
Heartbeat reads `HEARTBEAT.md`; an empty checklist suppresses work.

## Scheduling

The CLI has two explicit storage modes:

- no store option, or `--store PATH`: single-process JSON compatibility;
- `--runtime-db PATH`: schema-v12 SQLite jobs, attempts, leases, fences, retries, and
  recovery.

Use the second mode for new unattended work. The commands must all point to the same
database:

```bash
agnoclaw schedule add daily-report \
  --runtime-db ~/.agnoclaw/runtime.db \
  --schedule "0 8 * * *" \
  --timezone Asia/Dubai \
  --prompt "Generate the daily report" \
  --max-retries 2 \
  --retry-delay 30 \
  --retry-backoff 2 \
  --retry-max-delay 3600 \
  --retry-jitter 10 \
  --misfire-policy fire_once \
  --concurrency-key daily-report \
  --overlap-policy queue
agnoclaw schedule worker --runtime-db ~/.agnoclaw/runtime.db
agnoclaw schedule show daily-report --runtime-db ~/.agnoclaw/runtime.db
agnoclaw schedule trigger daily-report --runtime-db ~/.agnoclaw/runtime.db
agnoclaw schedule runs daily-report --runtime-db ~/.agnoclaw/runtime.db
agnoclaw schedule disable daily-report --runtime-db ~/.agnoclaw/runtime.db
```

The durable worker uses one immutable model/provider; `schedule add --runtime-db`
rejects per-job `--model`/`--provider` overrides. Run a separately configured worker
partition for another model. `--learning-consent` only propagates explicit consent into
the job's lifecycle call; normal learning scope, store, update-budget, candidate, and
promotion policy still apply. To activate the small CLI personal/session preset, start
the worker with `--learning-profile personal-session` and explicit trusted
`--tenant-id`, `--user-id`, and `--session`. JSON mode rejects this preset.

The SQLite CLI worker is the single-host deployment path. Multi-worker service hosts
compose `RuntimeSchedulerBackend(PostgresRuntimeStore(...))` in Python and own database
credentials, writer authority, tenant administration, artifacts, and supervision.
Schedule history records the distinct occurrence/attempt plus its linked
`runtime_run_id`. A worker crash or ambiguous acknowledgement reclaims or reattaches the
same attempt; only a known retryable terminal failure spends retry budget. Stale fences
cannot settle. Full semantics, Python composition, failure matrix, and security limits
are in [Durable scheduling](durable-scheduling.md).

The legacy JSON backend has no interprocess transaction or crash-safe ownership. Do not
run it concurrently and do not treat its atomic file replacement as a durable worker
lease.

## Durable run inspection

`inspect run` is the content-free operator view over the authoritative RuntimeStore:

```bash
export AGNOCLAW_TELEMETRY_IDENTIFIER_KEY='load-a-32-byte-secret-from-your-vault'

agnoclaw inspect run run_123 \
  --sqlite-db ~/.agnoclaw/runtime.db \
  --tenant-id tenant-42 \
  --user-id user-7

export RUNTIME_DSN='postgresql://...'
agnoclaw inspect run run_123 \
  --postgres-credential-env RUNTIME_DSN \
  --tenant-id tenant-42 \
  --user-id user-7 \
  --identifier-key-id 2026-q3 \
  --json
```

Choose exactly one backend. PostgreSQL and HMAC secrets are referenced by environment
variable **name**; there are no DSN/key-value flags. The command requires a current
runtime schema and exact user/optional tenant owner. SQLite opens through a read-only
URI with `query_only`; PostgreSQL skips schema migration and enforces read-only
transactions. The report includes HMAC-linked IDs, known state, bounded evidence
counts, terminal presence, and a recovery recommendation. It excludes prompts,
arguments, targets, metadata, output/error bodies, approval details, and artifact
addresses/content.

JSON success is schema `1.0` on stdout. Structured errors go to stderr: `77` means
not-found or owner denial without revealing which; `78` is configuration/schema/
dependency failure; `75` is transient store availability/capacity; `1` is another safe
typed inspection failure. A recommendation is explanatory—it does not authorize or
execute a lifecycle mutation. See [Observability and safe run
inspection](observability.md) for the SDK, privacy rules, and remaining gates.

## Migration automation

The local 0.12 migration commands are deliberately non-interactive and expose stable
schema-v1 JSON. A typical sequence is:

```bash
agnoclaw migrate 0.12 check OPTIONS --json
agnoclaw migrate 0.12 plan OPTIONS --output migration-plan.json --json
agnoclaw migrate 0.12 apply --plan migration-plan.json --state-dir STATE \
  --confirm-plan PLAN_DIGEST --writers-stopped --json
agnoclaw migrate 0.12 verify --state-dir STATE --json
agnoclaw migrate 0.12 cutover --state-dir STATE \
  --confirm-migration MIGRATION_ID --json
agnoclaw migrate 0.12 rollback --state-dir STATE \
  --confirm-migration MIGRATION_ID --writers-stopped --json
```

Success data goes to stdout; structured failures go to stderr. Exit `3` means a
precondition/confirmation/state blocker and exit `4` means integrity, drift, or
verification failure. The local workflow and its boundaries are in the
[operator runbook](migration-apply-0.12.md).

The PostgreSQL/service path exposes read-only `check`, `plan`, and `preview` plus the
explicitly confirmed `apply`, `verify`, `cutover`, and `rollback` lifecycle:

```bash
pip install "agnoclaw[cli,postgres,scheduler]"

agnoclaw migrate 0.12 service check \
  --source-credential-env AGNO_SOURCE_DSN \
  --target-learning-credential-env AGNO_TARGET_DSN \
  --target-runtime-credential-env AGNO_TARGET_DSN \
  --schedule-map-file private/schedule-map.json \
  --json

agnoclaw migrate 0.12 service plan \
  --source-credential-env AGNO_SOURCE_DSN \
  --target-learning-credential-env AGNO_TARGET_DSN \
  --target-runtime-credential-env AGNO_TARGET_DSN \
  --schedule-map-file private/schedule-map.json \
  --target-tenant-id tenant-a \
  --target-agent-id reviewer \
  --backup-receipt-id backup-object-v7 \
  --backup-receipt-digest "$BACKUP_RECEIPT_DIGEST" \
  --restore-test-id restore-drill-42 \
  --writer-fence-plan deployment-stop:v3 \
  --output migration-plan.json \
  --json

agnoclaw migrate 0.12 service preview \
  --plan migration-plan.json \
  --schedule-map-file private/schedule-map.json \
  --batch-size 1000 \
  --json

agnoclaw migrate 0.12 service apply \
  --plan migration-plan.json \
  --schedule-map-file private/schedule-map.json \
  --confirm-plan-digest "$PLAN_DIGEST" \
  --confirm-transform-digest "$TRANSFORM_DIGEST" \
  --confirm-backup-receipt-digest "$BACKUP_RECEIPT_DIGEST" \
  --confirm-writer-fence-plan deployment-stop:v3 \
  --writers-stopped --json

agnoclaw migrate 0.12 service verify \
  --plan migration-plan.json \
  --schedule-map-file private/schedule-map.json \
  --confirm-plan-digest "$PLAN_DIGEST" \
  --confirm-transform-digest "$TRANSFORM_DIGEST" \
  --confirm-writer-fence-plan deployment-stop:v3 \
  --writers-stopped --json

agnoclaw migrate 0.12 service cutover \
  --plan migration-plan.json \
  --schedule-map-file private/schedule-map.json \
  --confirm-plan-digest "$PLAN_DIGEST" \
  --confirm-transform-digest "$TRANSFORM_DIGEST" \
  --confirm-writer-fence-plan deployment-stop:v3 \
  --writers-stopped \
  --cutover-receipt-id deploy-2026-08-14 \
  --cutover-receipt-digest "$CUTOVER_RECEIPT_DIGEST" --json

agnoclaw migrate 0.12 service rollback \
  --plan migration-plan.json \
  --schedule-map-file private/schedule-map.json \
  --confirm-plan-digest "$PLAN_DIGEST" \
  --confirm-transform-digest "$TRANSFORM_DIGEST" \
  --confirm-writer-fence-plan deployment-stop:v3 \
  --writers-stopped \
  --confirm-no-post-cutover-target-writes --json
```

Only environment-variable **names** are accepted by the credential flags; resolved DSNs
stay inside the process. `check` and `plan` scan source and targets in bounded
`REPEATABLE READ, READ ONLY` transactions. `preview` verifies the exact plan with a
fresh scan, streams a second read-only source snapshot, recomputes source digests,
detects post-rekey collisions with a disk-backed bounded-memory registry, and returns
only transformed counts/digests. `apply` rechecks those exact digests under a new
read-only source snapshot, acquires target-schema advisory locks, and persists rows in
bounded provenance checkpoints. `verify` uses fresh connections and also proves that
data outside migration ownership has not changed. `cutover` records an external
deployment receipt but never edits deployment configuration. `rollback` reverses target
roles and deletes only exact rows classified as inserted. Every mutating command has
`--dry-run`, requires exact confirmation flags, and never prompts.

A blocker or state conflict exits `3`; integrity/drift exits `4`; retryable PostgreSQL
failures exit `75`; configuration/driver failures exit `78`. `plan` rescans, refuses an
existing file without `--overwrite`, and rejects symbolic-link destinations. Never pass
a service plan to the local SQLite/JSON commands. See the [PostgreSQL/service migration
runbook](migration-service-0.12.md) for cutover/rollback flags, control artifacts, and
open production-certification gates.

## General automation boundary

The current CLI is human-oriented. Stable JSON output, no-color mode, error explanation,
support bundles, and the final exit-code registry remain 0.12 release gates. Until those
land, Python APIs are the supported machine-to-machine interface and CLI output should
not be parsed as a stable protocol.

The current exceptions are `inspect run`, the versioned local migration workflow, and
all service migration JSON/exit contracts:

```bash
agnoclaw inspect run RUN_ID STORE_AND_OWNER_OPTIONS --json
agnoclaw migrate 0.12 check --learning-db PATH --schedules PATH --json
agnoclaw migrate 0.12 service check OPTIONS --json
agnoclaw migrate 0.12 service plan OPTIONS --output PLAN --json
agnoclaw migrate 0.12 service preview --plan PLAN --schedule-map-file MAP --json
agnoclaw migrate 0.12 service apply CONFIRMATION_OPTIONS --json
agnoclaw migrate 0.12 service verify CONFIRMATION_OPTIONS --json
agnoclaw migrate 0.12 service cutover CONFIRMATION_AND_RECEIPT_OPTIONS --json
agnoclaw migrate 0.12 service rollback CONFIRMATION_OPTIONS --json
```

For `check`, exit `0` means preflight-clear but does not assert writers are stopped;
exit `3` means semantic blockers. See
[0.12 migration preflight](migration-preflight.md).

## Configuration and troubleshooting

- [Configuration precedence and variables](configuration.md)
- [Compatibility and optional extras](compatibility.md)
- [Policy and guardrails](embedding/policy-and-guardrails.md)
- [Workspace files](workspace.md)
