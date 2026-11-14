# Banking v1 — provider-owned account contract

User-approved evolution 2026-11-14. Native UI renders server fields and sends actions; no account-selection policy or provider callback metadata is trusted client-side. No balances, full numbers, access tokens or provider Item/account/institution IDs appear in this contract. Sandbox is synthetic, not a real-bank/Production connection. Existing State, Plaid v1, experience and auth contracts are unchanged.

## Activation and exact interface

Explicit `--banking` / `banking_profile=True` opts into multi-Item Sandbox management and implies the Sandbox state profile. It composes with `--auth` and PostgreSQL. Without it the legacy profiles are unchanged. With it legacy `/connections/plaid` mutations return a sanitized 409 rather than overwriting multi-bank state; already committed keyed receipts can still replay their exact historical response. Use the versioned banking actions for new work. Once private format v2 is written, startup without this profile fails closed rather than attempting a single-Item downgrade. No public seed or public-token shortcut is added. Authenticated banking routes require the same verified issuer+subject boundary as every other data route.

- GET `/banking/v1`: no body/query; returns the schema root `$defs.banking` with `result=null`.
- POST `/banking/v1/actions`: discriminated `$defs.request` by `action`; returns the same root with typed nullable `$defs.result`.
- All mutations require schema_version=`1`, stable request_id, data_epoch and expected_revision from the latest backend State/banking/experience response. A stale revision returns 409 stale_banking; obsolete IDs/groups return 409 obsolete_selection/not_found. Refresh, do not silently retag/retry an obsolete selection. Exact successful retries replay their response; changed content under the same request ID conflicts. Mutation publication and receipts are atomic.

Every object rejects extra properties. Root fields: schema_version/data_epoch/revision/generated_at/environment/status/selection_notice/connections/result. Inventory is typed plain text, never executable UI or HTML. Public connection/account/group/session IDs are opaque app IDs, never provider IDs, and not human-visible labels. Client comparisons use epoch/revision, not timestamps. All data actions are per authenticated account; no user ID is accepted.

## Actions (all exact fields are normative in banking.schema.json)

| action | Additional required fields | Effect |
|---|---|---|
| add_link | consent_sandbox=true | Create a fresh add-bank native Link session without replacing existing Items. Returns result.link_session_id/link_token/expiration. |
| exchange | link_session_id, public_token | Add-mode success only: exchange one-use Sandbox token, fetch authoritative accounts/get inventory, commit a new private Item and new opaque connection. Every account returned by successful shared inventory starts enabled. |
| update_link | connection_id | Create native Link update mode with the existing access token and update.account_selection_enabled=true. Returns an opaque update session; no public access token is returned. |
| reconcile | connection_id, link_session_id | Update-mode success: call accounts/get for the existing Item. Never blindly exchange the callback's public token; callback account names/IDs are not request fields or authority. Every returned shared account is enabled, including previously app-disabled accounts. Accounts absent from a successful inventory become missing/unavailable. |
| cancel_link | link_session_id | Cancel the pending native session. Does not change existing sharing or disconnect an Item. |
| sync | connection_id (nullable) | Sync this connected Item or all connected Items when null, first fetching authoritative shared inventory, then importing enabled available accounts only. Partial failure is explicit; one Item's failure does not overwrite another. |
| disconnect | connection_id | Revoke only this whole Item via item/remove, never close bank accounts. Keep its stored history, exclude it from future imports/insights. Other Items remain connected. Unconfirmed remote removal is disclosed. |

`connections` includes retained disconnected entries so saved history remains understandable. Each has id/label/institution_name/status/can_manage/can_disconnect/last_inventory_at/last_sync_at/accounts/groups. Account fields: id/name/official_name/mask/type/subtype/availability/enabled/import_status/last_sync_at. Mask is a string, preserving leading zeros. Unknown provider type/subtype remain safely bounded strings/null; no guessing a known enum or invented institution. Missing inventory accounts become unavailable and disabled; absent fields do not imply permission or zero balances. Missing means no longer provider-linked/available; it does not invent a closure or revocation reason. Inventory failure leaves prior inventory and history intact and does not sync unknown accounts. Sync reports partial/attention; a failed reconcile returns an error without publishing partial inventory. `enabled` is a read-only server projection, never a local user preference. `can_enable` and the `select_accounts`/`select_group` actions are removed; such requests are rejected as invalid (422), with no frontend or legacy fallback.

`groups` is server-generated by type/subtype with opaque group ID and explicit current member IDs. Read-only headers may render labels; there are no group actions or account toggles. The selection_notice is backend-provided copy describing KEEP HISTORY / STOP FUTURE IMPORTS / EXCLUDE NEW INSIGHTS semantics. No native typing is required.

## Sync, history and privacy invariants

Private format v2 is an encrypted Item map, with each Item's token, private provider-to-opaque account mapping, account-scoped cursor, transaction mapping and provider-linked availability. Filtered transactions/sync uses options.account_id; a cursor belongs to exactly that Item/account stream. An old whole-Item cursor is never reused for an account stream. A returned shared account resumes its own stream (or starts it at null); retained transaction mapping deduplicates replay/backfill. Retention bounds stored history; expired records are not resurrected. Pending/posted links and genuine provider removals create immutable revisions as before; provider deselection/whole-bank disconnect never fabricates provider removals/tombstones.

Ledger history remains available, but every new deterministic card/evidence eligibility, experience inference/answer, notification plan and synthesis input excludes observations belonging to disabled/missing/disconnected accounts. Account display names/masks/institution labels and private provider mappings never enter model payloads. Source totals are aggregate counts only. Imported pages are staged; selection/epoch/context are rechecked before publishing, so late pages from a removed account cannot commit. Webhook jobs target their exact backend-owned Item, not a global last connection. Sync and reconciliation share the account service mutation/admission boundary. Inventory membership changes increment the private Item generation, fencing queued work while preserving per-account cursors.

The existing one-Item encrypted connection upgrades lazily and locally only when this explicit profile first opens it. Preserve the token, historical observations, keyed receipts, Item ownership and transaction mapping. Authoritative inventory reconciliation determines account associations; ambiguous historical records remain history-only rather than being assigned to a guessed account. Existing app-disabled accounts are auto-enabled only after a successful fresh `/accounts/get` response. This migration is callable through normal sync/reconcile, never through a cached read, callback metadata, or failed/malformed inventory. No Link reconfirmation gate remains: the user explicitly approved enabling all currently shared accounts. Legacy one-Item upgrade likewise uses successful inventory; account associations for retained history remain conservative. Inventory failure must not fabricate account association or erase history. No existing user, bank or database is remotely migrated by implementation/tests.

PostgreSQL ownership remains immutable by user for every Item, with per-Item generation fencing and snapshot/receipt/private-map publication in one transaction. SQLite remains explicit legacy/local mode. Whole application data deletion still erases bank private data/keys/pending sessions and attempts removal only with proven ownership; source revoke stops all Items. This remains a monolith with a durable private-volume dependency, not a stateless deployment.

## Handoff examples and errors

```json
{"schema_version":"1","request_id":"select-1","data_epoch":"banking-example-epoch","expected_revision":12,"action":"update_link","connection_id":"connection-opaque"}
```

On successful native add Link, send exchange with the returned opaque session ID and Sandbox public token. On successful native update Link, send reconcile with the existing connection ID and update session ID; ignore callback metadata for inventory. On exit/cancel send cancel_link when transport is available. Refetch on 409 and render the current server inventory. Controls may be disabled while a request is in flight; frontend must not synthesize successful changes offline.

Errors use `$defs.error`: sanitized 400 malformed JSON, 413 byte limit, 422 invalid_request/unsupported_version, 409 epoch_mismatch/stale_banking/obsolete_selection/idempotency_conflict/consent_required/busy/not_configured, 404 not_found and 500 internal_error. Native auth failures retain the separate auth-profile envelope/statuses. No provider responses, credentials, IDs or account metadata are echoed in errors.

## Plaid selection UX and scope

Initial selection (checking, savings, or both) belongs to native Plaid Link. Enable the Account Select pane for multiple accounts in the Plaid Dashboard customization used by the integration. `link_customization_name` can select a named customization; this implementation preserves the existing default Sandbox customization and does not invent an initial-Link API flag. Operator verification of the Dashboard/native experience remains necessary; this change does not alter live provider configuration.

Add bank remains the main CTA. Manage accounts opens native update Link, then onSuccess sends only `reconcile`; `/accounts/get`, not callback account metadata, determines sharing. Account removal/deselection through Link is institution-dependent: Chase requires its online Security Center. Never promise universal individual revocation. Whole-bank Disconnect is a separate confirmed action and uses `/item/remove`, retaining history rather than deleting it to simulate unlink.

`NEW_ACCOUNTS_AVAILABLE` is not authorization to import accounts missing from shared inventory. Automatic Manage prompting from that event is deferred to the webhook integration owner; the current inbox acknowledges non-transaction events without a prompt. Manage accounts remains available manually. Only accounts returned by successful shared inventory auto-enable.

Official references: https://plaid.com/docs/api/accounts/#accountsget and https://plaid.com/docs/link/update-mode/#using-update-mode-to-request-new-accounts . `/accounts/get` is Plaid's shared inventory (cached by Plaid, not necessarily a new bank fetch); “fresh” above means a new successful server API response, not a guaranteed bank-side refresh.
