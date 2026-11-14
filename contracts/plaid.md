# Plaid Sandbox contract — frozen additive profile 1

Frozen 2026-11-14. Scope: one additional route and a Sandbox-only state profile. No changes to state.schema.json, v1.md or scenarios.json. No implementation or production bank access is claimed. This is real Plaid API + real native Link, operating on synthetic bank fixtures, not fake UI success.

## Compatibility decision (mandatory)

Validated the existing state and observation-batch examples against the existing Draft 2020-12 schema with format checks before defining this profile. Existing provenance accepts only demo_fixture / healthkit_daily_steps / plaid_transactions and synthetic / daily_cumulative_sum / provider_record. Therefore the requested plaid_sandbox / transactions_sync values CANNOT pass the untouched base schema. Do not silently relabel them or use allOf to widen a closed enum.

plaid.schema.json defines a separate opt-in state profile: all base state fields and all purchase fields remain identical; only observation validation uses the additional strict Sandbox provenance branch. It references unchanged base definitions, not a copied divergent domain model. All full-state responses in an opted-in deployment, including GET /state and existing mutations, MUST validate against plaid.schema.json#/$defs/state. Legacy clients validating only state.schema.json cannot consume Plaid observations. schema_version remains "1" as requested; profile selection is a deployment/build agreement, NOT wire auto-negotiation. Base-only deployments retain the original contract. This explicit incompatibility requires parent integration adoption, not edits to frozen base files.

The opt-in profile overrides only the old prose prohibitions on configured Plaid, Plaid fixture ingestion through this adapter, and purchase-source selection. All immutable evidence, feedback, retention, source consent, epoch, deterministic rules and notification safeguards remain. Existing /observations is NOT a route for client-supplied Plaid observations or Plaid source_updates.

## Transport and typed interface

POST /connections/plaid; local loopback HTTP only, no auth or public deployment. JSON UTF-8; body limit 1 MiB. Every declared property is required; nullable fields are explicit null; reject unknown fields at every object depth. Root schema validates requests. Success validates $defs.response; full state validates $defs.state; errors reuse $defs.error (base error envelope). No additional routes, webhook receiver, jobs route, provider metadata, bank/account identifiers, cursors, access tokens or raw payloads to the client.

Common request: schema_version="1", request_id (base opaque ID syntax), data_epoch (current state epoch), action. Discriminator selects exactly:

| action | Additional required request properties | result in HTTP 200 |
|---|---|---|
| link_token | consent_sandbox=true | link_token, expiration |
| exchange | public_token (Sandbox token returned by Link success) | status="connected" |
| sync | none | status=waiting/partial/complete/error; added, modified, removed, skipped integers; retry_after_seconds integer 1..300 or null |
| disconnect | none | status=disconnected/removal_pending |

Every success envelope has exactly schema_version, request_id, data_epoch, action, environment="sandbox", result, state (complete State). Outer data_epoch equals state.data_epoch. request_id echoes caller, never Plaid's request ID. Link token is the sole temporary client-facing secret in a response; expiration is the actual provider timestamp. No client access_token anywhere. No counts in exchange: exchange does not imply any transaction is ready. Schema JSON examples follow below.

## Four action semantics

1. **link_token:** User explicitly selects “Connect Plaid Sandbox (synthetic bank data)” and consents. Reject missing server configuration with 409 not_configured. Serialize a single connection reservation per epoch; reject another connected Item or in-flight connection with 409 busy. Grant plaid app consent, enable plaid in preferences, set authorization_requested=true and read_access=not_applicable. Call only https://sandbox.plaid.com/link/token/create with client_name="Local Companion", language="en", country_codes=["US"], products=["transactions"], transactions.days_requested=90, user.client_user_id=server-generated opaque epoch-scoped ID. Never use email, real identity, or caller-provided host/products. No redirect_uri is needed for the selected non-OAuth Sandbox test flow. A cancelled Link creates no Item: source remains enabled/granted but empty/not connected; disconnect is available, and a new link_token request may replace an expired/abandoned reservation. Existing active native session must be dismissed before replacing it.
2. **exchange:** Only actual Link success.publicToken enters the app route. Require active epoch's server-side connection reservation and consent. Reject an unsolicited token with 409 consent_required. Exchange at /item/public_token/exchange. Persist encrypted access token and private Item mapping before reporting connected. Opaque app-generated observation identities hide provider IDs. Set source status=syncing, last_attempt_at=now, last_success_at=null, coverage=unknown. Do not auto-import on exchange: frontend invokes sync. One Item maximum. public_token is short-lived and must not be logged, copied to preferences, or persisted in cleartext request receipts. Same successful request retry returns its receipt without exchanging again. A transport-ambiguous exchange or crash before durable token commit is NOT automatically repeated: invalidate the local reservation, expose a sanitized error and require fresh Link; a remotely created orphan Item is a disclosed cleanup limitation.
3. **sync:** Require connected Item plus enabled/granted consent (409 consent_required when disabled; 404 not_found when no Item exists). Server calls /transactions/sync with saved cursor (omit on first call), count=500, no account_id filter, no raw original description request. Fetch all pages before committing. result counts describe committed normalized new revisions in that cycle, not raw provider totals. added is new upsert identity; modified is next upsert revision of existing identity; removed is a new tombstone; skipped is provider entries producing no revision (including out-of-retention/unsupported/duplicate). Waiting/error cycles with no commit have zero counts. An unchanged successful provider result does not create duplicate observation revisions.
4. **disconnect:** Revoke consent, remove plaid from enabled_source_ids, invalidate the connection generation and pending Link reservation immediately, cancel derived intents/drafts, recompute purchase card without Plaid. Call /item/remove with server-only token. On confirmed removal erase token/cursor/private mappings and return disconnected. If remote removal fails, return removal_pending and source status=error with sanitized reason; retain only encrypted revocation material for an explicit later disconnect retry with a NEW request_id. Never collect while revoked. If no Item/reservation exists, disconnect is idempotent. Historical normalized observations stay immutable/visible but excluded until retention or DELETE /data. New connection is blocked while removal is pending.

Capability available means installed Sandbox adapter with configured Sandbox credentials, not successful Link or product entitlement. Failed/missing configuration => not_configured, not simulated available. The credential probe was NOT performed in this research task. Backend must verify actual credentials before claiming access. On Plaid HTTP 401, stop, disable provider use and return 409 not_configured; never switch environments or retry alternative credentials. For non-auth provider failures, source.status=error and sanitized source.reason; sync returns status=error and a bounded retry hint only for transient failures. Do not expose provider error bodies. Client mistakes use base 400/413/422/404/409 mappings; transient local infrastructure failure uses base 500 internal_error.

## Pagination, readiness, retries and races

Plaid added/modified/removed arrays are a change stream, not a snapshot. Capture cursor C0 and connection generation before the first page. Stage normalized results and opaque mappings privately; when has_more=false, atomically commit all revisions, source freshness, deterministic recomputation, idempotent receipt and final next_cursor. Never advance cursor before evidence commit; never expose intermediate pages. Preserve C0 across the entire attempt. TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION means discard every staged page and restart from C0, at most twice per request; exhaustion is sync error with retry_after_seconds=5 and no evidence/cursor changes. Other page failures also discard staging. Budget one request to 25 seconds, <=20 pages; exceeding either fails safely without cursor advancement. No /transactions/refresh requirement.

has_more=false does NOT establish that historical import is done. Map transactions_update_status: NOT_READY => waiting; INITIAL_UPDATE_COMPLETE => partial; HISTORICAL_UPDATE_COMPLETE => complete; TRANSACTIONS_UPDATE_STATUS_UNKNOWN or omitted => waiting when no evidence, partial otherwise. Set source.status=syncing for waiting/partial; complete => ready when active observations exist, empty otherwise. Complete is provider import readiness, not full coverage or proof of consumer behavior. Coverage stays unknown/partial, never complete. Successful empty polls set last_success_at but are not invented zero-purchase evidence. source coverage_window reflects imported evidence dates or null; never assert coverage from requested history alone.

Frontend automatically polls only while foreground and user remains connected: delays 2, 4, 8, 16, 30 seconds, five automatic attempts maximum, then visible manual “Sync again”. New logical polling attempt uses NEW request_id; a transport retry of the same request uses SAME body/request_id. Return retry_after_seconds=2 for waiting/partial (frontend applies max(server hint, its backoff)); complete returns null; error is 5 if transient, otherwise null. No tight polling, background promises, or public webhooks.

Serialize connection work per Item; concurrent conflicting work returns 409 busy, except revocation/deletion must invalidate generation promptly rather than wait to publish old results. Recheck epoch, generation, enabled consent, current cursor and deletion state after every network boundary and again inside the final DB transaction. A stale worker cannot commit tokens, observations, cursor, source status, receipts or drafts. Exchange completing after revocation/deletion must attempt /item/remove using only its in-memory token and then discard it; failure is an unconfirmed remote cleanup limitation, not permission to resurrect local data.

Idempotency remains (data_epoch, route, request_id) + canonical-body digest; action is part of body. Do not persist public_token in request receipts; persist a keyed digest. Link-token receipts must be encrypted at rest and excluded from logs; identical retry returns original result, including possibly expired token, so clients request a new ID when expired. Revocation/deletion invalidates pending token usability; delete erases these sensitive receipts. No stale-epoch receipt may return an old token after deletion. Existing deletion replay exception returns only its clean deletion response.

DELETE /data must invalidate all connection generations, cancel workers/reservations and attempt bounded /item/remove for every locally held Item before erasing tokens and all revocation material, mappings, cursors and old receipts; rotate epoch under existing rules regardless of provider availability. If removal was not confirmed, clean state's limitations says "Remote Sandbox Item removal unconfirmed." Do not retain tokens in a hidden cleanup queue after delete or claim remote deletion succeeded. Preference revocation has the same immediate exclusion/generation safety and triggers remote cleanup; permission re-add alone does not recreate a deleted Item. Demo reset must reject any active/pending Plaid consent or connection, including revoked removal_pending material.

## Exact observation projection

source_id="plaid", mode="fixture", kind="purchase", provenance.adapter="plaid_sandbox", adapter_version="1", method="transactions_sync", coverage="partial" (or unknown for no usable coverage). provenance.record_id and observation.id are stable opaque app IDs, not Plaid transaction/account/Item IDs. Maintain encrypted private mapping scoped by epoch and connection; IDs never reused after deletion/reconnect. First revision=1; a semantic modification increments latest revision by one and supersedes its exact predecessor. No-op metadata changes do not create a revision.

Use Plaid authorized_date when present, otherwise date. Date-only => time_precision="date", occurred_at=null; timezone is preferences.timezone captured at import (an application bucketing convention, not institution timezone evidence). pending=true => lifecycle=pending, posted_date=null; otherwise lifecycle=posted and posted_date=date. purchase.authorized_date keeps provider authorized_date, including null. Missing usable dates are skipped, never filled with now/midnight. Skip older-than-retention records rather than feed invalid batches; held/skipped counts and source limitation disclose this. Existing date-only observations are not silently rebucketed on timezone changes.

purchase has EXACTLY category, merchant, authorized_date, posted_date, is_refund; activity=null. merchant is Plaid merchant_name only, truncated to 120 Unicode code points, null if absent; never fall back to raw transaction name/description. household_attribution="unknown" initially, never infer self from account ownership. A merchant or banking category is NOT a meal, consumption, diet or health fact.

Category mapping requires matching personal_finance_category.primary="FOOD_AND_DRINK" AND detailed value. FOOD_AND_DRINK_RESTAURANT or FOOD_AND_DRINK_FAST_FOOD => restaurant; FOOD_AND_DRINK_GROCERIES => grocery. Other recognized classifications => other. Missing/malformed/inconsistent categories => null. Coffee, alcohol stores, vending and miscellaneous food are not restaurant by inference. Provider category labels are merchant transaction classifications, not verified purchases of a specific food.

Plaid amount is a signed number in the transaction's currency units (NOT integer cents): positive outflow, negative inflow. Keep decimal precision in transient parsing; never multiply/round to cents or perform currency conversion. Currency is iso_currency_code or unofficial_currency_code; unknown currency is not USD. Base purchase has NO amount/currency fields: do NOT add them to observation/response, and do not retain unnecessary raw amount data. Exclude zero/negative entries from purchase ingestion unless a negative FOOD_AND_DRINK purchase-category entry is retained conservatively as is_refund=true (a credit/refund candidate, not proof of refund). Positive purchase entries set is_refund=false. Never treat arbitrary deposits/payments as refund evidence. Unknown currency does not affect count-only classification but must not support spending claims.

Removed provider IDs with a known active observation create operation=remove, lifecycle=removed, purchase=null, activity=null, revision=latest+1, supersedes=latest; preserve identity/date/provenance. Unknown removed IDs are skipped. Pending-to-posted links use ONLY pending_transaction_id, never merchant/date similarity. Resolve across the entire staged update and stored mapping before applying changes. For a new posted ID replacing a known pending ID, emit posted revision=1 superseding the pending latest reference. When that pending ID is also in removed, suppress its separate tombstone: the posted successor itself deactivates the pending record, avoiding two active successors. If pending removal was committed previously, link to its latest tombstone reference. A posted ID already known follows its own same-ID revision chain. Deduplicate repeated provider IDs deterministically; contradictory same-cycle payloads fail the cycle rather than guess. All evidence references resolve and remain acyclic; old revisions persist except retention/deletion.

Purchase card selects active consented plaid fixture evidence when Plaid is enabled/granted and connected (even if empty), otherwise demo. Never merge sources or fixture/live evidence in a card. Unknown household attribution permits observed purchase counts but prevents personal eligibility until explicit feedback establishes self. Preserve existing scenario thresholds and safe language. No cloud upload follows from bank consent.

## Builder boundaries / acceptance

Frontend: decode discriminated envelope, run actual native Link, pass only success.publicToken, retain no access token, replace entire state, respect epochs/backoff and clear old tasks on disconnect/delete. Backend: safe .env parser, encrypted Item store, Link adapter, atomic sync projector, typed route and full-state profile validation. Neither side needs raw Plaid metadata from the other.

Use official SwiftPM dependency https://github.com/plaid/plaid-link-ios-spm.git pinned EXACT 7.4.0 (verified release 2026-11-12). Product/module LinkKit. Native session API: LinkTokenConfiguration(token:onSuccess:onExit:onLoad:), Plaid.createPlaidLinkSession(configuration:), retain PlaidLinkSession, present session.sheet() in SwiftUI (or session.open(using:)). Do NOT copy legacy Plaid.create/Handler APIs or the example's token-printing callbacks. iOS 17 Simulator is supported by the supplied arm64/x86_64 simulator slices; runtime/build still must be tested locally. No Hosted Link fallback is needed merely because this is a Simulator. Use non-OAuth Sandbox institution and user_good/pass_good; no real bank or Apple Card/FinanceKit flow.

API-only E2E may use Plaid /sandbox/public_token/create with initial_products=["transactions"] and documented default user_good/pass_good, then private backend test harness exchange/sync/remove. This bypass is ONLY for API tests; app /exchange expects Link-origin token in app operation, and UI acceptance MUST actually launch LinkKit and complete it. No test-token creation endpoint in the app and no fake “connected” button. An API test cannot attest UI success.

Required tests: all request/response examples; strict nested unknown fields; wrong environment/public-token prefix; stale epoch/consent; exchange duplicate/ambiguous failure; added/modified/removed and pending/posted same-page and cross-page; mutation pagination restart; no partial commit; empty initial history; disconnect failure; delete during Link/exchange/sync; exact same-ID retries; expired link token; old token receipt erasure; category/amount exclusions; no secrets in HTTP/state/logs. No backend or iOS implementation was changed by this contract task.

Free OpenRouter synthesis is a separate parent-owned requirement: no new LLM route/model choice is frozen here, no provider key reading, no paid fallback, and no implied cloud consent. Existing /process + cloud_consent and synthetic-only bounds remain. Plaid connection and deterministic ledger must work without any LLM.

## Literal JSON examples

The following synthetic request/response pairs are schema-valid protocol examples, NOT minted usable tokens or evidence of a provider call. Responses carry full state; empty ledger arrays are permitted by the base schema (a builder may keep its three insufficient-evidence cards). All actual state-producing routes must return their current complete ledger, not discard it to match these examples.

### link_token

Request:

```json
{
  "schema_version": "1",
  "request_id": "plaid-1",
  "data_epoch": "sandbox-epoch-1",
  "action": "link_token",
  "consent_sandbox": true
}
```

Response (full snapshot):

```json
{
  "schema_version": "1",
  "request_id": "plaid-1",
  "data_epoch": "sandbox-epoch-1",
  "action": "link_token",
  "environment": "sandbox",
  "result": {
    "link_token": "link-sandbox-example-not-usable",
    "expiration": "2026-11-14T14:00:00Z"
  },
  "state": {
    "schema_version": "1",
    "data_epoch": "sandbox-epoch-1",
    "revision": 1,
    "generated_at": "2026-11-14T10:00:00Z",
    "evaluation_date": "2026-11-14",
    "preferences": {
      "food_suggestions": true,
      "activity_suggestions": true,
      "suppressed_scenario_ids": [],
      "enabled_source_ids": [
        "plaid"
      ],
      "timezone": "America/New_York",
      "notifications": {
        "enabled": false,
        "local_time": "18:00"
      },
      "no_kitchen": false,
      "no_spending": false,
      "movement_restriction": false,
      "notes": "",
      "cloud_consent": false
    },
    "sources": [
      {
        "id": "demo",
        "enabled": false,
        "consent": "not_requested",
        "capability": "available",
        "status": "disabled",
        "authorization_requested": false,
        "read_access": "not_applicable",
        "last_attempt_at": null,
        "last_success_at": null,
        "coverage": "unknown",
        "coverage_window": null,
        "reason": null
      },
      {
        "id": "healthkit",
        "enabled": false,
        "consent": "not_requested",
        "capability": "available",
        "status": "disabled",
        "authorization_requested": false,
        "read_access": "unknown",
        "last_attempt_at": null,
        "last_success_at": null,
        "coverage": "unknown",
        "coverage_window": null,
        "reason": null
      },
      {
        "id": "plaid",
        "enabled": true,
        "consent": "granted",
        "capability": "available",
        "status": "empty",
        "authorization_requested": true,
        "read_access": "not_applicable",
        "last_attempt_at": null,
        "last_success_at": null,
        "coverage": "unknown",
        "coverage_window": null,
        "reason": "Awaiting Link completion."
      },
      {
        "id": "email",
        "enabled": false,
        "consent": "not_requested",
        "capability": "not_configured",
        "status": "not_configured",
        "authorization_requested": false,
        "read_access": "not_applicable",
        "last_attempt_at": null,
        "last_success_at": null,
        "coverage": "unknown",
        "coverage_window": null,
        "reason": null
      }
    ],
    "observations": [],
    "ledger": [],
    "feedback": [],
    "notification_intents": [],
    "process": {
      "run_id": null,
      "status": "idle",
      "started_at": null,
      "completed_at": null,
      "input_state_revision": 0,
      "ledger_status": "idle",
      "synthesis": {
        "mode": "disabled",
        "status": "idle",
        "reason": null,
        "engine": "deepagents",
        "drafts": []
      }
    },
    "limitations": [
      "Synthetic Plaid Sandbox evidence only."
    ]
  }
}
```

### exchange

Request:

```json
{
  "schema_version": "1",
  "request_id": "plaid-2",
  "data_epoch": "sandbox-epoch-1",
  "action": "exchange",
  "public_token": "public-sandbox-example-not-usable"
}
```

Response (full snapshot):

```json
{
  "schema_version": "1",
  "request_id": "plaid-2",
  "data_epoch": "sandbox-epoch-1",
  "action": "exchange",
  "environment": "sandbox",
  "result": {
    "status": "connected"
  },
  "state": {
    "schema_version": "1",
    "data_epoch": "sandbox-epoch-1",
    "revision": 2,
    "generated_at": "2026-11-14T10:00:00Z",
    "evaluation_date": "2026-11-14",
    "preferences": {
      "food_suggestions": true,
      "activity_suggestions": true,
      "suppressed_scenario_ids": [],
      "enabled_source_ids": [
        "plaid"
      ],
      "timezone": "America/New_York",
      "notifications": {
        "enabled": false,
        "local_time": "18:00"
      },
      "no_kitchen": false,
      "no_spending": false,
      "movement_restriction": false,
      "notes": "",
      "cloud_consent": false
    },
    "sources": [
      {
        "id": "demo",
        "enabled": false,
        "consent": "not_requested",
        "capability": "available",
        "status": "disabled",
        "authorization_requested": false,
        "read_access": "not_applicable",
        "last_attempt_at": null,
        "last_success_at": null,
        "coverage": "unknown",
        "coverage_window": null,
        "reason": null
      },
      {
        "id": "healthkit",
        "enabled": false,
        "consent": "not_requested",
        "capability": "available",
        "status": "disabled",
        "authorization_requested": false,
        "read_access": "unknown",
        "last_attempt_at": null,
        "last_success_at": null,
        "coverage": "unknown",
        "coverage_window": null,
        "reason": null
      },
      {
        "id": "plaid",
        "enabled": true,
        "consent": "granted",
        "capability": "available",
        "status": "syncing",
        "authorization_requested": true,
        "read_access": "not_applicable",
        "last_attempt_at": null,
        "last_success_at": null,
        "coverage": "unknown",
        "coverage_window": null,
        "reason": null
      },
      {
        "id": "email",
        "enabled": false,
        "consent": "not_requested",
        "capability": "not_configured",
        "status": "not_configured",
        "authorization_requested": false,
        "read_access": "not_applicable",
        "last_attempt_at": null,
        "last_success_at": null,
        "coverage": "unknown",
        "coverage_window": null,
        "reason": null
      }
    ],
    "observations": [],
    "ledger": [],
    "feedback": [],
    "notification_intents": [],
    "process": {
      "run_id": null,
      "status": "idle",
      "started_at": null,
      "completed_at": null,
      "input_state_revision": 0,
      "ledger_status": "idle",
      "synthesis": {
        "mode": "disabled",
        "status": "idle",
        "reason": null,
        "engine": "deepagents",
        "drafts": []
      }
    },
    "limitations": [
      "Synthetic Plaid Sandbox evidence only."
    ]
  }
}
```

### sync

Request:

```json
{
  "schema_version": "1",
  "request_id": "plaid-3",
  "data_epoch": "sandbox-epoch-1",
  "action": "sync"
}
```

Response (full snapshot):

```json
{
  "schema_version": "1",
  "request_id": "plaid-3",
  "data_epoch": "sandbox-epoch-1",
  "action": "sync",
  "environment": "sandbox",
  "result": {
    "status": "waiting",
    "added": 0,
    "modified": 0,
    "removed": 0,
    "skipped": 0,
    "retry_after_seconds": 2
  },
  "state": {
    "schema_version": "1",
    "data_epoch": "sandbox-epoch-1",
    "revision": 3,
    "generated_at": "2026-11-14T10:00:00Z",
    "evaluation_date": "2026-11-14",
    "preferences": {
      "food_suggestions": true,
      "activity_suggestions": true,
      "suppressed_scenario_ids": [],
      "enabled_source_ids": [
        "plaid"
      ],
      "timezone": "America/New_York",
      "notifications": {
        "enabled": false,
        "local_time": "18:00"
      },
      "no_kitchen": false,
      "no_spending": false,
      "movement_restriction": false,
      "notes": "",
      "cloud_consent": false
    },
    "sources": [
      {
        "id": "demo",
        "enabled": false,
        "consent": "not_requested",
        "capability": "available",
        "status": "disabled",
        "authorization_requested": false,
        "read_access": "not_applicable",
        "last_attempt_at": null,
        "last_success_at": null,
        "coverage": "unknown",
        "coverage_window": null,
        "reason": null
      },
      {
        "id": "healthkit",
        "enabled": false,
        "consent": "not_requested",
        "capability": "available",
        "status": "disabled",
        "authorization_requested": false,
        "read_access": "unknown",
        "last_attempt_at": null,
        "last_success_at": null,
        "coverage": "unknown",
        "coverage_window": null,
        "reason": null
      },
      {
        "id": "plaid",
        "enabled": true,
        "consent": "granted",
        "capability": "available",
        "status": "syncing",
        "authorization_requested": true,
        "read_access": "not_applicable",
        "last_attempt_at": null,
        "last_success_at": null,
        "coverage": "unknown",
        "coverage_window": null,
        "reason": null
      },
      {
        "id": "email",
        "enabled": false,
        "consent": "not_requested",
        "capability": "not_configured",
        "status": "not_configured",
        "authorization_requested": false,
        "read_access": "not_applicable",
        "last_attempt_at": null,
        "last_success_at": null,
        "coverage": "unknown",
        "coverage_window": null,
        "reason": null
      }
    ],
    "observations": [],
    "ledger": [],
    "feedback": [],
    "notification_intents": [],
    "process": {
      "run_id": null,
      "status": "idle",
      "started_at": null,
      "completed_at": null,
      "input_state_revision": 0,
      "ledger_status": "idle",
      "synthesis": {
        "mode": "disabled",
        "status": "idle",
        "reason": null,
        "engine": "deepagents",
        "drafts": []
      }
    },
    "limitations": [
      "Synthetic Plaid Sandbox evidence only."
    ]
  }
}
```

### disconnect

Request:

```json
{
  "schema_version": "1",
  "request_id": "plaid-4",
  "data_epoch": "sandbox-epoch-1",
  "action": "disconnect"
}
```

Response (full snapshot):

```json
{
  "schema_version": "1",
  "request_id": "plaid-4",
  "data_epoch": "sandbox-epoch-1",
  "action": "disconnect",
  "environment": "sandbox",
  "result": {
    "status": "disconnected"
  },
  "state": {
    "schema_version": "1",
    "data_epoch": "sandbox-epoch-1",
    "revision": 4,
    "generated_at": "2026-11-14T10:00:00Z",
    "evaluation_date": "2026-11-14",
    "preferences": {
      "food_suggestions": true,
      "activity_suggestions": true,
      "suppressed_scenario_ids": [],
      "enabled_source_ids": [],
      "timezone": "America/New_York",
      "notifications": {
        "enabled": false,
        "local_time": "18:00"
      },
      "no_kitchen": false,
      "no_spending": false,
      "movement_restriction": false,
      "notes": "",
      "cloud_consent": false
    },
    "sources": [
      {
        "id": "demo",
        "enabled": false,
        "consent": "not_requested",
        "capability": "available",
        "status": "disabled",
        "authorization_requested": false,
        "read_access": "not_applicable",
        "last_attempt_at": null,
        "last_success_at": null,
        "coverage": "unknown",
        "coverage_window": null,
        "reason": null
      },
      {
        "id": "healthkit",
        "enabled": false,
        "consent": "not_requested",
        "capability": "available",
        "status": "disabled",
        "authorization_requested": false,
        "read_access": "unknown",
        "last_attempt_at": null,
        "last_success_at": null,
        "coverage": "unknown",
        "coverage_window": null,
        "reason": null
      },
      {
        "id": "plaid",
        "enabled": false,
        "consent": "revoked",
        "capability": "available",
        "status": "disabled",
        "authorization_requested": true,
        "read_access": "not_applicable",
        "last_attempt_at": null,
        "last_success_at": null,
        "coverage": "unknown",
        "coverage_window": null,
        "reason": null
      },
      {
        "id": "email",
        "enabled": false,
        "consent": "not_requested",
        "capability": "not_configured",
        "status": "not_configured",
        "authorization_requested": false,
        "read_access": "not_applicable",
        "last_attempt_at": null,
        "last_success_at": null,
        "coverage": "unknown",
        "coverage_window": null,
        "reason": null
      }
    ],
    "observations": [],
    "ledger": [],
    "feedback": [],
    "notification_intents": [],
    "process": {
      "run_id": null,
      "status": "idle",
      "started_at": null,
      "completed_at": null,
      "input_state_revision": 0,
      "ledger_status": "idle",
      "synthesis": {
        "mode": "disabled",
        "status": "idle",
        "reason": null,
        "engine": "deepagents",
        "drafts": []
      }
    },
    "limitations": [
      "Synthetic Plaid Sandbox evidence only."
    ]
  }
}
```
