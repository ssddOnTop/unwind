# Local Companion backend

A single-user Python 3.12/FastAPI service with durable SQLite state. No authentication, accounts, provider keys, or network services are required for the complete deterministic workflow. The supported runner binds **127.0.0.1 only**, uses one worker, rejects non-loopback Host headers, disables proxy-header trust/access logs, and does not install CORS middleware. Do not expose this server publicly. A physical iPhone's loopback is not the Mac; the initial supported topology is iOS Simulator to Mac.

## Run

From the repository root:

```sh
./be/run-local.sh
# Optional isolated local database and port, still under the repository:
./be/run-local.sh --port 8001 --data-dir "$PWD/be/.data-demo"
```

The script uses `uv run --frozen`, creates the environment in `be/.venv`, and sets `UV_CACHE_DIR`, `UV_PYTHON_INSTALL_DIR`, `PYTHONPYCACHEPREFIX`, and `TMPDIR` under the repository root. Install the `uv` command first if it is not already available. Python must be 3.12; the verified interpreter was CPython 3.12.12. The supported launcher does not accept a host override. POSIX file locking prevents two service processes from concurrently owning the same database. SQLite files are mode 0600; the launcher uses umask 077.

For a direct invocation after dependencies are installed:

```sh
be/.venv/bin/python -m companion --port 8000
```

Open the local generated documentation at `http://127.0.0.1:8000/docs`; the generated OpenAPI document is `http://127.0.0.1:8000/openapi.json`. There is no alternate ReDoc, OAuth redirect, jobs, login, or debug route. Swagger's browser assets use FastAPI's default CDN; the backend itself needs no external network for deterministic or scripted processing.

## Plaid Sandbox opt-in

The default remains the original eight-route/base-schema deployment. To opt into the separately frozen Sandbox state profile and ninth route:

```sh
./be/run-local.sh --plaid-sandbox
# Equivalent explicit runtime setting: COMPANION_PLAID_SANDBOX=1
```

Only explicit runtime entry points (the launcher and the opt-in API probe) load the root `.env` using python-dotenv; importing modules, creating a test app, or running pytest never loads it. The file must be a regular mode-0600 file and is never logged. Existing exported variables take precedence. Configure `PLAID_CLIENT_ID` and `PLAID_SANDBOX_SECRET` (or `PLAID_SECRET`). Every request is pinned to `https://sandbox.plaid.com`, regardless of environment labels; there is no Production fallback. HTTP 401 disables further provider use until configuration is corrected/restarted. No credentials are necessary for the original deterministic fixture flow.

Opted-in responses from **all** state-producing routes use the additive Plaid state profile. The unchanged base schema cannot validate `plaid_sandbox` / `transactions_sync` provenance. Use the profile agreed with the frontend; do not switch back to base-only mode on a populated Plaid database before deleting data in the opted-in deployment.

`POST /connections/plaid` is a typed action-discriminated route: `link_token` with `consent_sandbox=true`, `exchange` with the native Link success public token, `sync`, and `disconnect`. All requests include schema_version/request_id/current data_epoch. Only the temporary Link token is client-visible; access tokens, Item IDs, transaction mappings and cursors never enter HTTP state, logs, preferences, or model inputs. Manual preferences cannot forge a connected Item and `/observations` cannot import client-forged Plaid evidence.

The Plaid capability uses a fixed-origin bounded transport, an encrypted private vault and transaction coordinator outside core. Authenticated ciphertext for access token/cursor/mappings joins the authoritative SQLite snapshot, so final cursor and normalized observation revisions commit atomically. A separate mode-0600 key stays under the selected data directory. Sensitive Link receipts are encrypted and public-token receipts retain only keyed digests. Exchange staging is encrypted and fsynced before reporting success; ambiguous exchange failures require a fresh Link session rather than automatic token reuse.

Sync stages every page before publication (25-second total budget, 20-page total cap, at most two pagination restarts from the original cursor). Added/modified/removed deltas produce immutable revisions and exact pending-to-posted links; unchanged entries do not create revisions. Provider status, not `has_more=false`, controls waiting/partial/complete readiness. Date-only times stay unknown, initial household attribution is unknown, verified PFC mapping is narrow, and no amount/currency/raw description is retained. Signed Decimal parsing is transient only. A connected Plaid purchase card never sums demo purchases, even before the first import; bank consent never grants cloud consent. Connecting exits the fixed demo clock.

Disconnect/preference revocation immediately excludes evidence and invalidates in-flight workers before bounded remote removal. Failure returns removal_pending and retains only encrypted revocation material for an explicit retry. DELETE rotates epoch, erases local tokens/key/cursors/mappings/receipts and attempts remote Item removal; unconfirmed removal is disclosed rather than reported as success. Historical normalized observations remain on disconnect, but DELETE erases them. While the server runs, consented connected Items receive at most one automatic sync attempt per elapsed 24 hours; manual foreground sync remains available. This does not promise background execution while the server is off.

The explicit `be/probe-plaid-sandbox.py` harness is API-only and uses the documented Sandbox public-token shortcut. It is **not** a native Link UI acceptance test and is never an app endpoint. It uses isolated repository-local temporary data, creates only synthetic transactions, attempts Item cleanup even on failure, and emits only status/count evidence. The verified probe imported 12 normalized observations and counted 4 restaurant records; import readiness was still waiting, not falsely complete. Remote removal and local deletion were confirmed. Native Link presentation/completion must still be verified by the frontend owner.

## Frozen HTTP interface

Base mode has exactly eight domain routes, all successful responses HTTP 200:

| Method | Path | Request | Response |
| --- | --- | --- | --- |
| GET | `/health` | none | capability metadata |
| GET | `/state` | none | complete state |
| POST | `/observations` | observation batch + source updates | accepted/ignored counts + state |
| POST | `/feedback` | scoped correctness/usefulness/suppress | state |
| PUT | `/preferences` | full preference replacement | state |
| POST | `/process` | disabled/scripted/live synthesis mode | state |
| POST | `/demo` | seed/reset, representative-v1, explicit demo consent | state |
| DELETE | `/data` | JSON confirm=delete_all | clean state in new epoch |

All writes require the current `schema_version`, `request_id`, and `data_epoch`; first GET state. Use `application/json`. Every schema field is required, nullable values are explicit null, unknown fields are rejected, and bodies are capped at 1 MiB. GET endpoints take no query parameters or body. The frozen contract and JSON Schema in the sibling contracts directory remain authoritative and are read at runtime; retain that directory beside this source checkout. The package is not a standalone relocatable wheel containing copied contracts.

Malformed JSON returns 400, oversized bodies 413, schema/semantic errors 422, missing references 404, and conflicts 409. Framework errors use the same sanitized error envelope. Errors do not echo observations, notes, provider messages, or stack traces. Mutation retries use the same request ID and canonical body; a different body conflicts. A retry returns the originally committed response, not the newest snapshot. New writes using a pre-deletion epoch are rejected.

## First end-to-end demo

1. GET `/state` and copy the returned epoch.
2. POST `/demo` with `action=seed`, `fixture_set=representative-v1`, `consent_demo=true`, and a new request ID.
3. Observe three restaurant purchases on three dates spanning 19 days; 13,800 recorded steps on three of seven completed dates; movement suggestions suppressed by the synthetic fixture's explicit preference. These are product rules, not health assessments.
4. POST `/process` with `synthesis_mode=scripted` to execute the actual Deep Agents graph using a scripted model and coverage-review subagent, without keys/network. Output is explicitly labeled scripted and cannot replace ledger facts.
5. POST event correctness feedback with attribution `shared` for a purchase. Original evidence remains unchanged; the personal suggestion loses eligibility. Restore with explicit `correct` + `self`, or suppress the scenario persistently.
6. DELETE `/data` with `confirm=delete_all`. The epoch rotates, observations/preferences/feedback/jobs/checkpoints/receipts/intents are erased, and nothing automatically reseeds.

Seed is a no-op on an unchanged representative fixture and preserves later preferences/feedback. Modified fixture observations or non-demo history/consent cause demo_conflict. Reset is demo-only, erases prior demo state, and rotates the epoch. Exact deletion/reset retries safely replay the clean receipt. A demo-only session uses its fixed clock. Granting a non-demo source consent or accepting live evidence permanently switches that session to the real application clock, including retention; revocation and later pruning cannot switch it back. Delete starts a clean session.

For accepted ingestion instead of seed: explicitly save the fresh preferences (granting demo consent), substitute the current epoch in the frozen example batch, then POST it. Replaying that batch after seed ignores all six records.

## Extension boundaries

Core contains only `Extension` Protocol, registry metadata, duplicate-registration checks, and versioned async dispatch. It contains no SQL, rules, prompts, HTTP, or startup composition.

Every registered capability exposes `id`, `version="1"`, `status`, and `async handle(operation, payload) -> dict`. Registered IDs are persistence, scenarios, feedback, demo, healthkit, plaid, email, synthesis. Payloads and results are JSON domain objects validated against the frozen schema, not private implementation types. `create_app(data_dir, replacements={capability_id: replacement}, scheduler=..., clock=...)` supports test/conformance replacement. The injection arguments are server/test composition only, never HTTP clock/provider controls.

Public operations:

- persistence: `load` returns `{state, meta}` or `{}`; `receipt` receives epoch/route/request_id and returns digest/response or `{}`; `commit` atomically stores document and receipt, optionally erasing receipts/recovering interrupted work; `close` releases the connection/process lock.
- scenarios: `evaluate({state}) -> {state}` computes deterministic cards; no database access.
- feedback: `apply({state, request, created_at}) -> {state}` appends correction records and explicit suppression preferences; no observation mutation.
- demo: `seed`/`reset` receive `{state, fixture_session}` and return `{state, changed}`; fixture source validation is also supported.
- source connections: `validate_observation({observation, evaluation_date})` and `validate_update(source_update)` return `{valid:true}` or a sanitized domain error. HealthKit is a normalized read-only adapter boundary, not proof of Apple authorization or device access. Plaid/email remain not_configured.
- synthesis: `SynthesisExtension(checkpoint_path)`; `synthesize({mode, run_id, data_epoch, state_revision, state})` returns exactly the synthesis schema. `clear({})` deletes SDK checkpoints and returns `{cleared:true}`. An explicit unavailable adapter handles a missing module; it does not fabricate scripted success.
- scheduler: independently injectable `handle("plan", {state, issued, now}) -> {intents, issued}`. Notification issuance history is committed with state, not held only in memory.

The application service coordinates one mutation lock and atomic state/receipt transactions. It admits a persistent running job and captures the input revision **before releasing the lock for synthesis**. GET and preference/source/delete mutations remain responsive during model work. Concurrent processing is busy; publication rechecks epoch/context and validates all draft references/filters. Delete cancels the task before checkpoint erasure. Provider errors, timeouts, and invalid output do not erase the ledger. Restart changes a persisted running job to failed/internal_error without automatically replaying paid work.

Observations are immutable revisions with exact supersedes links; daily aggregates are unique per source/date/timezone. Feedback is a separate overlay. Source revocation removes evidence from current calculations while preserving historical records until retention/deletion. No missing dates are zero-filled. Timezone changes exclude incompatible date-only aggregates rather than rewriting them.

Housekeeping runs every 60 seconds outside core and on startup/writes: 90-local-day observation retention, linked feedback/draft cleanup, current-day evaluation, source freshness, and notification expiration. GET /state itself never changes revision/time. The opt-in GET /experience/v1 first reconciles housekeeping so it cannot infer from yesterday's stale evaluation; it changes the revision only if that reconciliation changes the committed document. Notifications are off by default, generic, at most one issuance per local date with a seven-local-date suggestion cooldown. Cancelled intents cannot be regenerated that day. DST gaps are skipped and repeated times choose the first occurrence.

SQLite uses DELETE journaling, secure_delete and VACUUM after privacy erasure; prior response caches are removed. This is application-level erasure, **not a guarantee of secure overwrite of SSD blocks, operating-system snapshots, backups, or external copies**. Unrelated research fixture checkpoint files are not application data and are not touched.

## Optional backend-owned experience

Run `./be/run-local.sh --experience` from the repository root, or combine `--experience --plaid-sandbox`. This enables only GET `/experience/v1` and POST `/experience/v1/answers`; it does not change the old State schema. The explicit Python factory option is `experience_profile=True`. Base-only OpenAPI retains eight paths; experience has ten; experience with Plaid has eleven. There is no new demo/scenario API or demo flag. Original `/health` keeps its eight frozen capability entries.

The normative contract is `contracts/experience.schema.json`, documented in `contracts/experience.md`. The backend owns the date, verified evidence selection, possible-habit inference, Home priority and concise copy, visit brief, question key, eligibility and cadence. The native client renders typed plain-text fields and submits one of the supplied answer options using the current epoch/revision/key. No typing, local inference, local answer store or model call is needed. Purchase attribution confirmed by a tap is distinct from unconfirmed consumption/eating routine; absent/stale/suppressed information is never a healthy/all-clear label.

The independently replaceable `experience` extension exposes `project`, `answer`, and `clean` through the same registry interface. Payloads contain public State, private service-scoped answers, server time/evaluation date, review readiness and selected sources; results contain the typed projection/question scope or cleaned answer list. Persistence and HTTP remain outside that extension. Answers are atomically stored with state revision and idempotency receipt in private SQLite metadata, not State or model input. Exact-evidence keys survive polling, row order, card-revision changes and rolling windows that retain the same evidence. A changed key never inherits an answer. Automatic questions have a seven-elapsed-day cooldown after any answer; voluntary correction remains available. Retention is bounded to 90 days/128 answers. Revocation and epoch deletion remove affected answers and invalidate private response receipts.

This is legacy isolated-instance mode, not managed-account authentication. Parent-owned verified Clerk composition must supply isolated per-user service/database/vault/checkpoint/receipt/answer scopes; no client-provided identity field is accepted here. Do not expose this unauthenticated runner as a shared-account server or silently migrate anonymous data to the first signed-in user.

The five experience verification scenarios use real synthetic observations and ordinary domain admission in isolated tests, not canned screens or a runtime client scenario generator. Examples and full test evidence are written under `evidence/backend-experience/`. Synthetic provenance is preserved in Home and copied visit briefs. No live network or provider key is needed for this pipeline.

## Optional daily processing

Daily model processing is disabled by default. Set `COMPANION_DAILY_SYNTHESIS_MODE=scripted` for a no-network fixture demo, or `live` to use the existing provider and consent gates. The lifespan worker checks immediately at startup and after each housekeeping pass. It attempts processing only when an eligible, consented suggestion has evidence and at least 24 elapsed UTC hours have passed since the last recorded attempt; failed/skipped and manual attempts count too. The persisted process record prevents restart bursts. Missed intervals do not accumulate catch-up work, and the server must be running for checks to execute. Scripted processing rejects live observations; live HealthKit transmission remains blocked.

## Dependencies and optional environment

The reproducible `uv.lock` includes the verified orchestration pins from the research lock:

- deepagents 0.7.38
- langgraph 1.2.22
- langchain 1.5.0
- langchain-openai 1.8.0
- langgraph-checkpoint-sqlite 3.1.3
- aiosqlite 0.22.1
- pydantic 2.12.5

FastAPI, Uvicorn, httpx, jsonschema format validation, pytest and pytest-asyncio are also installed/locked. The SDK/checkpointer dependency set resolved and the actual scripted graph/checkpoint tests passed together in the backend environment.

No provider environment is required. Optional live fixture-only synthesis requires all of:

- `COMPANION_LLM_PROVIDER=openrouter`;
- `COMPANION_LLM_MODEL=openrouter/free` or an explicitly catalog-verified zero-priced `:free` tool-capable model;
- backend-only `OPENROUTER_API_KEY` (Requesty/paid-model fallback is disabled);
- saved `cloud_consent=true`.

Never put keys in app requests, source control, logs, or notes. The authorized mode-0600 root `.env` is loaded only by the explicit local launcher; tests/imports do not read it. Missing configuration yields skipped/no_credentials. Consent false yields skipped/cloud_consent_required after credentials are checked. Live HealthKit transmission remains unapproved even with cloud consent. The graph is bounded to 30 seconds, four model calls, four tool calls, 8,000 input and 1,000 output tokens across subagents. Live gateway calls are not part of the acceptance run and remain unverified.

## Tests

From `be/`:

```sh
UV_CACHE_DIR="$PWD/../.cache/uv" uv sync --frozen
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q companion
```

Pytest temporary files/caches stay beneath the repository. Tests use local temporary databases, an injected clock, in-process HTTP or the actual local server, and synthetic evidence. Coverage includes fixture equality, generated OpenAPI, malformed/oversize JSON, strict fields/dates/units, atomic rejection, duplicate/revision/supersedes/tombstone rules, consent, feedback overlays, immediate filters, restart, retention, deletion/reset, timezone/DST/coverage, notification cooldowns, process concurrency/cancellation/stale publication, failing/replacement extensions, and actual SDK execution with network connection attempts denied.

The final backend result record is in the repository evidence directory. Passing software tests are not clinical validation. Apple HealthKit entitlement/read permission, physical-device connectivity/background collection, OS notification delivery, paid gateway access, Production Plaid/real-bank access, live email, backups and secure media overwrite are not established by these tests. Sandbox API verification is separate from native Link UI verification. Daily synthesis is explicitly opt-in as described above; deterministic housekeeping does not depend on model availability.

## Explicit PostgreSQL monolith profile

This remains one Python FastAPI monolith, not Workers, serverless services or an AWS deployment choice. Local SQLite remains an explicit default. `--auth --persistence postgres` selects PostgreSQL with no SQLite fallback and a separate default private-volume root `be/.data-auth-pg`. It requires the existing Clerk verifier configuration and a backend-only `DATABASE_URL`; neither selecting a registered user nor importing anonymous data is automatic. Existing authentication admission and all protected data routes remain unchanged. No frontend receives the database URL or a database credential.

From the repository root (commands are operator actions, not automatically executed):

```sh
# Read-only sanitized diagnostics; loads the authorized runtime environment only here.
./be/.venv/bin/python -m companion.postgres_admin check
# Explicit dedicated-schema migration. NOT executed against the supplied remote database.
./be/.venv/bin/python -m companion.postgres_admin migrate --confirm-private-schema
# After successful operator migration and configuration:
./be/run-local.sh --auth --persistence postgres --plaid-sandbox --data-dir "$PWD/be/.data-auth-pg"
```

Use a direct PostgreSQL connection or a **session-mode** pooler. Transaction-mode poolers are incompatible with lifetime session advisory locks; the conventional transaction-pooler port 6543 is rejected, and other transaction-pooler endpoints must not be configured. Production configuration requires verified TLS (`sslmode=verify-full`); system trust roots are used unless `sslrootcert` supplies an explicit CA. Insecure modes are rejected. The isolated test fixture alone permits plaintext loopback connections to its newly created disposable PostgreSQL cluster. Connections, pool waits and statements have bounded timeouts. Pool capacity defaults to 16; at most 8 resident user services reserve sessions, with remaining capacity for identity and inbox operations. Capacity/ownership contention fails closed rather than silently evicting users or switching stores.

The dedicated non-public `companion_private` schema stores globally stable `(verified issuer, verified subject) -> UUID` identities; account-scoped JSONB snapshots and receipts; immutable Sandbox Item ownership; and verified-event receipts/jobs. Snapshot JSON includes private **encrypted** Plaid connection/cursor metadata, visit answers, preferences, observations, scheduling and processing state. A single transaction commits snapshot, receipt, ownership activation/revocation and ownership-generation fencing. Identity comes only from verified Clerk claims or an internal worker's already persisted ownership mapping; untrusted webhook `user_id` never selects an account. Source revoke/deletion disables owned Items and erases their jobs/notification receipts. Minimal inactive Item ownership tombstones remain to prevent cross-account reassignment; they are not usable tokens. Remote Item removal additionally checks that this exact account owns the Item, including for pending-exchange cleanup: an ownership collision cannot let deletion of B remove A's Item. If ownership was never committed or cannot be checked, cleanup fails closed and remains explicitly unconfirmed; a possible orphan then needs operator reconciliation. Identity mapping also survives app-data deletion. No current test user is selected or migrated implicitly.

Every account snapshot/receipt query binds its internal account UUID. A lifetime session advisory lock serializes that account across processes without holding a database transaction across Plaid/model awaits. A second process cannot open the same account while the first owns it; it returns a temporary unavailable error. A lost session is not silently reconnected into a stale in-memory service: writes fail until the handle is deliberately closed/restarted. A private-volume file lock additionally fences residual key/checkpoint files across workers sharing that volume. This is conservative ownership, not transparent load balancing or automatic failover; route/drain workers accordingly. Verified-key caches may be shared, but mutable services/providers/vaults remain account-local.

RLS is enabled and forced for snapshots and receipts, using a transaction-local account setting in addition to explicit SQL predicates. The trusted application role performs global identity and scheduler/ownership metadata operations. It is not a client-access role. A superuser or BYPASSRLS role bypasses PostgreSQL RLS; application predicates and verified admission still matter, and RLS is not claimed to constrain such a supplied role. The migration revokes PUBLIC and existing Supabase `anon`/`authenticated` schema/table access; do not add this schema to PostgREST exposure or grant API roles access. An operator should provision a least-privilege application role and review deployment grants.

### Verified Plaid webhook inbox

Set a server-only HTTPS `PLAID_WEBHOOK_URL` with PostgreSQL and Sandbox enabled. It is included only in newly created Link tokens; existing Items are **not** automatically updated. Updating an existing Item requires a separate explicit operator action. No API public-token shortcut or demo route is added. Native Link remains the application connection flow; prior API-only Sandbox probes are not native Link verification.

Only POST `/webhooks/plaid` bypasses Clerk, and it must pass the separate Plaid signature verifier before any inbox effect. Body buffering is capped at 64 KiB before verification; the fixed Sandbox key endpoint streams at most 8192 bytes, rejects duplicate/nonfinite JSON, verifies TLS and follows no redirects. Unknown keys map to 401; transient verification/storage failures map to retryable 503. A 200 means a durable verified inbox commit, **not** completed transaction import. No transaction sync runs inline with webhook delivery.

The inbox hashes exact raw bytes as supplied by the verifier, deduplicates for five minutes, bounds receipts/jobs to 10,000 live rows each, and quarantines unknown Items for ten minutes without creating accounts. Exchange visibility races reconcile only against immutable server-owned Item mappings. Current supported transaction notifications coalesce into monotonically requested/completed counters. Notifications arriving during a running lease request another pass, even for equal raw bodies. Two bounded monolith worker tasks use SKIP LOCKED claims, 360-second leases, a 300-second callback deadline, generation/epoch checks, bounded exponential retry and a twelve-attempt ceiling. Fresh notifications can rearm an idle failed job; owned jobs expire after 24 hours. Interrupted leases are recoverable after expiry. Failed/exhausted jobs are not represented as successful imports. The worker rechecks ownership inside the normal Plaid admission lock before vendor effects; disconnect/deletion fences late work.

The supported launcher still binds only 127.0.0.1 and does not create a public HTTPS endpoint. A future conventional deployment needs an explicitly configured TLS reverse proxy/Host policy and private-volume routing; no public delivery or deployment is claimed here. No existing server/port was restarted to implement this profile.

### Residual durable-volume dependencies and limits

This is **not a stateless/full storage migration**. Each internal account UUID still needs its private directory on a durable, correctly isolated volume for:

- `plaid.key`: mode-0600 vault encryption/HMAC key; missing keys cannot decrypt persisted PostgreSQL ciphertext and fail closed;
- `plaid-pending.vault` and its atomic temporary file: encrypted one-use exchange crash recovery, erased after the PostgreSQL transaction commits;
- SDK `checkpoints.sqlite` and sidecars: synthesis checkpoints, cleared by the existing privacy flow;
- `.postgres-service.lock` and the dedicated-root marker: local residual ownership/admission, not authoritative observations.

Do not deploy multiple machines with unrelated ephemeral copies of these files and claim failover. A shared/private durable-volume strategy or a separately designed managed-key/checkpoint migration remains required. The private volume and PostgreSQL need coordinated backup/restore policies; secure media overwrite and backup deletion are not proven by SQL deletion. Existing anonymous or SQLite account directories are rejected rather than silently adopted into the PostgreSQL residual root.

Tests use a newly initialized isolated local PostgreSQL server and disposable databases only; never the ambient `DATABASE_URL`. If no server binary is available these tests explicitly skip instead of masquerading as real DB verification. PostgreSQL test evidence and sanitized read-only remote diagnostics are under `evidence/postgres-monolith/`. Live hosted migration, public webhook delivery, native Link callbacks and conventional-host TLS configuration remain separate release checks.

## Container monolith (local demo, no deployment)

From the repository root, copy `container.env.example` to `.env`, restrict it to mode
0600, and fill the empty values privately. Then run `docker compose up --build -d`.
One nonroot process/one Uvicorn worker serves authenticated PostgreSQL + banking +
experience, including the existing signed Plaid webhook inbox/tasks; no separate
worker, queue or database container is started. Daily LLM synthesis is disabled by
default; optional free-model settings use the existing provider validation.

`DATABASE_URL` must use a direct/session-mode PostgreSQL endpoint and verified TLS;
`CLERK_ISSUER`, Sandbox credentials and HTTPS `PLAID_WEBHOOK_URL` are required.
Optional `CLERK_AUTHORIZED_PARTIES` is a JSON array; the actual audience variable is
`CLERK_AUDIENCE` (omit it when unused). No anonymous/SQLite fallback or automatic
DDL/migration/account seeding is performed. The existing startup check requires an
already migrated schema. Do not run this against the migration owner's database
until that owner approves the coordinated handoff.

Set `SUPABASE_CA_FILE` to the existing host CA (for example the operator's
`/Users/ssdd/hackpctn/prod-ca-2021.crt`). Compose mounts it read-only at
`/run/certs/supabase-ca.crt`; the entrypoint overrides only the DSN's `sslrootcert`
with this container path, never embeds a host path or certificate in the image.
Set `COMPANION_DATA_DIR` to the **prepared PostgreSQL residual root**, with its
existing `.postgres-root` marker and `users/<UUID>/` keys, pending exchanges and
SDK checkpoints. The bind source must already exist, be owned by UID/GID 10001,
and be mode 0700; private files must remain mode 0600. Coordinate ownership/copying
with the migration owner, never recursively chown an active other worktree.
The launcher sets umask 077 and refuses a missing/unprepared root. It never
copies or regenerates existing keys; the existing vault fails closed when a
persisted token's key is missing. **Losing this volume means existing bank tokens
cannot be decrypted**, even if PostgreSQL survives. Back up/restore both together.
Do not scale this service or run concurrent copies of the migrated residuals.

The image preserves `/app/be/companion` and `/app/contracts` so source-relative
contract loading works. Only the seven required public JSON contracts (including
normative examples imported by the backend) enter the image; no frontend, tests,
private data, dotenv, certificates or evidence enters the build context. Existing
locked project dependencies include pytest; packaging does not rewrite that lock.

Container binding is `0.0.0.0:8000`, published only as `127.0.0.1:8000` on the host.
`curl --fail http://127.0.0.1:8000/health` is a pure liveness check returning only
`{"status":"ok"}`, not a database/provider readiness probe. Existing localhost
Host validation is unchanged: a future operator-managed TLS reverse proxy must
send `Host: 127.0.0.1:8000` upstream, preserve Authorization and signed webhook
bodies, and terminate public HTTPS itself. Forwarded headers remain untrusted.
The local Compose demo alone cannot receive public Plaid webhooks. No native
endpoint settings, public server, cloud resource or deployment is changed here.

## Explicit multi-bank management profile

`--banking` / `banking_profile=True` enables the frozen additive `contracts/banking.schema.json` and `contracts/banking.md` interface: GET `/banking/v1` and POST `/banking/v1/actions`. It implies Sandbox support and composes with authentication, PostgreSQL and `--experience`. No frontend fixture, client-owned selection policy, new public-token shortcut, account seeding or public migration route is added. Credentials and models are not needed to run the offline tests. Existing processes and data are not changed merely by installing this code.

After operator-approved database/schema setup, the intended conventional-monolith command is:

```sh
./be/run-local.sh --auth --persistence postgres --banking --experience --data-dir "$PWD/be/.data-auth-pg"
```

This is documentation, not an instruction to restart an existing instance or migrate its connected user. SQLite remains supported for explicitly isolated local use. No PostgreSQL schema/version bump is needed for banking: existing version-1 snapshot/receipt/immutable Item-ownership tables support multiple Items. A hosted schema that is not yet installed still needs the existing explicit `companion.postgres_admin migrate --confirm-private-schema` operator action. No remote migration, write or diagnostic is run by the banking tests.

All nine discriminated actions require the current epoch/revision and a stable request ID: `add_link`, `exchange`, `update_link`, `reconcile`, `cancel_link`, `select_accounts`, `select_group`, `sync`, `disconnect`. Native add-mode Link success sends the opaque session and Sandbox public token to `exchange`. Native update-mode success sends the existing opaque connection and update session to `reconcile`, which reads authoritative `/accounts/get`; callback account names/IDs never establish authority. Update mode uses the existing access token and `update.account_selection_enabled=true`, not a blind token exchange. Link cancellation leaves existing banks and choices intact. Server-configured HTTPS webhook URLs apply only to new Link sessions; no existing Item is silently updated.

New/discovered accounts are disabled until explicitly selected. Subtype batches apply only to the exact existing opaque account-ID set rendered at the submitted revision, never future discoveries. Disabling an account is **local removal**: keep immutable stored observations in State/history, stop that account's future transaction imports and exclude its records from new deterministic cards, experience, notification planning and synthesis input. It does not claim individual bank-side access revocation. Whole-bank disconnect alone attempts `/item/remove`, only for that Item, and never closes a bank account. Failed remote removal is disclosed and can be retried.

Each Item has an encrypted token and private provider-to-opaque account mapping. Each selected account has its own filtered `/transactions/sync` cursor and retained transaction mapping; Item cursors are never reused for account streams. Explicit re-enable resumes the account's cursor or starts at null and deduplicates retained backfill. Retention-expired IDs are fenced against resurrection. Every page is staged; generation/epoch/selection are rechecked before publication. Partial failures preserve successful stream cursors and history, report attention and conservatively suppress new cross-account insights until selected streams are ready. A disabled/missing/disconnected account never receives transaction sync requests.

Opening a prior one-Item store under this profile performs an explicit local private-format-v2 upgrade. It retains the encryption key, token, observations, keyed receipts and immutable PostgreSQL ownership. It does not choose a Clerk user or move any user between databases. Old Item-wide consent cannot prove a per-account choice, so inventory accounts conservatively require explicit selection; ambiguous history stays history-only until a filtered replay proves association. A staged legacy one-use exchange is recovered only in its matching epoch, and its pending file is cleared only after the upgraded snapshot commits. Inventory failure does not erase history or invent associations. Previously disconnected/pending-removal Items are not reactivated. Once v2 is written, a legacy-only writer fails startup rather than silently downgrading it; back up the coordinated database and private directory before any intentional operator rollout.

PostgreSQL atomically publishes the full encrypted Item map, snapshot, receipt and every Item's immutable user ownership/generation. An inactive immutable ownership reservation precedes Item-specific inventory requests, so an Item collision cannot call another user's inventory/sync/remove endpoints. Verified webhook jobs target the exact owned Item and share normal admission/selection fencing. Whole-source revoke fences all Items before remote awaits; deletion rotates the epoch, fences all Items/jobs and drains key/session cleanup even if its caller is cancelled. Other users' snapshots, keys, receipts and jobs are unchanged.

The previously documented durable-volume/session-pooler, TLS, role/RLS, public-hosting and backup limitations still apply. Banking is not stateless. Account names, official names, masks and institution labels appear only in the authenticated management projection/encrypted receipts and private map, never in public State, aggregate source summaries or model input. Test evidence is under `evidence/backend-banking/`; actual disposable PostgreSQL tests are distinguished from mock Plaid/JWKS transport, and native Link/live provider/hosted deployment remain release checks.
