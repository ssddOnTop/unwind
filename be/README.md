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

## Frozen HTTP interface

Exactly eight domain routes, all successful responses HTTP 200:

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

Housekeeping runs every 60 seconds outside core and on startup/writes: 90-local-day observation retention, linked feedback/draft cleanup, current-day evaluation, source freshness, and notification expiration. GET itself never changes revision/time. Notifications are off by default, generic, at most one issuance per local date with a seven-local-date suggestion cooldown. Cancelled intents cannot be regenerated that day. DST gaps are skipped and repeated times choose the first occurrence.

SQLite uses DELETE journaling, secure_delete and VACUUM after privacy erasure; prior response caches are removed. This is application-level erasure, **not a guarantee of secure overwrite of SSD blocks, operating-system snapshots, backups, or external copies**. Unrelated research fixture checkpoint files are not application data and are not touched.

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

- `COMPANION_LLM_PROVIDER` explicitly `openrouter` or `requesty`;
- `COMPANION_LLM_MODEL` explicitly chosen;
- the corresponding backend-only `OPENROUTER_API_KEY` or `REQUESTY_API_KEY`;
- saved `cloud_consent=true`.

Never put keys in app requests, files, source control, logs, or notes. No `.env` file is automatically loaded. Missing configuration yields skipped/no_credentials. Consent false yields skipped/cloud_consent_required after credentials are checked. Live HealthKit transmission remains unapproved even with cloud consent. The graph is bounded to 30 seconds, four model calls, four tool calls, 8,000 input and 1,000 output tokens across subagents. Live gateway calls are not part of the acceptance run and remain unverified.

## Tests

From `be/`:

```sh
UV_CACHE_DIR="$PWD/../.cache/uv" uv sync --frozen
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q companion
```

Pytest temporary files/caches stay beneath the repository. Tests use local temporary databases, an injected clock, in-process HTTP or the actual local server, and synthetic evidence. Coverage includes fixture equality, generated OpenAPI, malformed/oversize JSON, strict fields/dates/units, atomic rejection, duplicate/revision/supersedes/tombstone rules, consent, feedback overlays, immediate filters, restart, retention, deletion/reset, timezone/DST/coverage, notification cooldowns, process concurrency/cancellation/stale publication, failing/replacement extensions, and actual SDK execution with network connection attempts denied.

The final backend result record is in the repository evidence directory. Passing software tests are not clinical validation. Apple HealthKit entitlement/read permission, physical-device connectivity/background collection, OS notification delivery, paid/live gateway access, live Plaid/email, backups and secure media overwrite are not established by these tests. Daily synthesis is explicitly opt-in as described above; deterministic housekeeping does not depend on model availability.
