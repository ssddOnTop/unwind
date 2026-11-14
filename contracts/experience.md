# Backend experience contract v1 — frozen

Frozen November 14, 2026 for the user-approved thin-native-client architecture. This is additive: state.schema.json, scenarios.json, v1.md and the Plaid contract are unchanged. No clinical validation is claimed. The backend, not iOS, owns inference, eligibility, source selection, evaluation date, Home priority/copy, brief generation, question identity/cadence and persisted answers.

## Activation and exact routes

Explicit launch flag `--experience` enables exactly two additional domain routes. It composes with `--plaid-sandbox`. Without this flag these routes are absent (404); original base-only eight-route behavior remains. No runtime demo/scenario-generation route exists; fixture scenarios are internal tests/operator-import inputs only. No new public port, authentication, webhook or provider/model dependency is introduced.

| Route | Request | HTTP 200 response |
| --- | --- | --- |
| GET `/experience/v1` | No body or query | `$defs.experience` (schema root) |
| POST `/experience/v1/answers` | `$defs.answer_request` | `$defs.experience` |

`experience.schema.json` is normative Draft 2020-12, with format validation. Every object rejects unknown fields; every property except the additive inference_history field is required; nullable properties are explicit null. Strings are plain text, never HTML, Markdown rendering instructions, URLs, scripts or executable UI. Native UI may expand/collapse, navigate by the finite action kind, display typed values, and copy the already prepared brief on explicit tap. It must not recalculate patterns, choose clinical interpretations, fabricate absent fields, generate answer keys, merge source counts, or infer no-flags status. Existing source collection, protected transport/outbox mechanics, native Link/HealthKit permissions and OS notification APIs remain native infrastructure.

Root fields: schema_version="1", data_epoch, revision, generated_at, evaluation_date, timezone, is_demo, provenance (synthetic/live/mixed/none), home, concern, clarification (nullable), visit, inference_history (optional for older responses). `revision` is the authoritative committed State snapshot counter, including answer writes; generated_at is its commit time, not a client clock. The client discards a lower revision within the same epoch and clears old views/answers/tasks on epoch rotation. The response intentionally contains no raw observations, merchant names, provider identifiers, vault data, feedback notes, preference notes, model prose, or provider IDs. History includes normalized observation references, not provider identifiers. Epoch and clarification/action identifiers are protocol metadata, never visible copy.

`home` contains status, headline, detail, info (secondary caveats for an info affordance), sample_label and nullable primary_action. Action.kind is connect/view_visit/view_ledger/explore_suggestion; label/detail are backend copy, card_id/suggestion_id are nullable routing metadata. Concern contains an explicit status, concise title/summary, and nullable counts/first_date/last_date/span_days; clients display these values without deriving their own threshold.

`visit` contains a generated title/caption, record-review scope, reason, optional nonurgent starting point, scope notice, observed_information, inferred_habits, confirmed_context, user_reported_preferences, uncertainties, questions, and complete plain-text brief. Observed, inferred and tap-confirmed attribution remain separate sections. No typing is required. Optional view-local cosmetic edits/clipboard handling are not authoritative answers or evidence and must not replace backend inference. A synthetic brief starts with a visible sample-data marker; the client must preserve it when copying.

## Inference and concise copy

The existing operational rule remains rule_version 1.0.0: restaurant-category posted, non-refund, active observations within D-27...D, at least three distinct qualifying dates spanning at least 14 calendar days. This is a transparent product recurrence setting, not a dietary/medical threshold. Evidence links, actual totals, source consent/capability/readiness, selected source, windows, timezone, and latest correctness/usefulness feedback are revalidated server-side. D comes from the backend evaluation clock. A running/failed deterministic review, stale source/date, ambiguous/missing refs, mismatched totals or unknown HealthKit read access cannot become an all-clear.

The backend may automatically offer the **unconfirmed** interpretation “Your recent purchases suggest you may be eating out often.” when self/unknown-attributed qualifying evidence meets the rule. Shared/other-attributed or disputed records do not establish that personal pattern; they may remain in observed totals. Event partial feedback without explicit attribution is disputed. Correctness/card and usefulness/card feedback remain distinct. Food-disabled/suppressed/unavailable patterns do not create a new interpretation or question. Movement suppression retains valid recorded-step facts but suppresses movement suggestions. Step gaps are unknown, not zeros or inactivity.

Home prioritizes a supported possible purchase habit, then eligible supported suggestions, then supported recorded steps, then purchase facts. No-pattern copy is exactly scoped: “No patterns flagged in available records”. This state requires both supported categories to have current verified summaries, no hidden/disputed/unavailable category, and a below-threshold purchase pattern; partial coverage still remains explicit in secondary information. It is not returned merely because evidence is absent, stale, suppressed or corrected away. It is not a health assessment, normal/healthy label or future-risk estimate. A No answer uses corrected status, not no_flags.

No generated disease, clinical-risk score, diagnosis, nutritional quality, need for a specialist or personal consumption certainty is inferred from purchases. Primary care/family medicine is an optional nonurgent conversation starting point, not a recommendation that the user needs treatment. Urgent symptoms are not assessed by this feature. Secondary caveats live in home.info and expanded brief, not a long Home warning paragraph.

## Optional answer mutation and validity

Example request (opaque values must be obtained from the current response):

```json
{"schema_version":"1","request_id":"answer-1","data_epoch":"experience-example-epoch","expected_revision":12,"clarification_key":"purchase-attribution:opaque-fingerprint","answer":"not_sure"}
```

The only accepted values are `yes`, `no`, `not_sure`, `dismissed`. The clarification includes key/question/scope/answer/should_prompt/can_answer/next_prompt_at/options. Render options and their labels directly. The question is “Were these purchases mostly for you?” and its scope names the actual qualifying record counts/dates, not consumption.

- Yes records user-confirmed purchase attribution only. The eating habit and consumption remain unconfirmed; the immutable observations, ledger evidence and existing personal-suggestion eligibility are not rewritten.
- No removes the personal habit interpretation while preserving observed facts. It does not produce no-flags or health reassurance.
- Not sure preserves uncertainty, with user-reported context but no false confirmation.
- Dismissed saves the choice not to answer and adds no claimed personal fact.

Responses persist in backend SQLite metadata, scoped by epoch and an opaque fingerprint of the exact active evidence revisions, selected source, rule/timezone and effective attribution/correction semantics. Arbitrary state revision, card revision, ordering, process runs, notes, and a shifted evaluation window that still contains exactly the same qualifying evidence do not by themselves replace the key. Changed/removed evidence or attribution creates a new key and invalidates the old answer for current interpretation. Old answers are never automatically applied to new evidence. This intentionally fixes the former client key's daily-window/card-revision churn.

Any answer stops automatic prompting for that exact key. A materially new key within seven elapsed days of the last valid answer/dismissal has answer=null and should_prompt=false; it remains voluntarily answerable (can_answer=true) and supplies next_prompt_at. After that cooldown a new unanswered key can prompt. Polling cannot itself create a new key or reset cadence. Server-held records are bounded to 128 and 90 days; references erased by retention, source revocation, deletion or reset remove affected answer data. Re-adding a source does not restore a purged answer. No client-local legacy answer cache is trusted or silently imported.

Every mutation checks exact current epoch, expected_revision and current clarification key before writing. `expected_revision` mismatch => 409 stale_experience; obsolete/unavailable key => 409 obsolete_pattern. The client refreshes experience and offers the current optional question, not an automatic retag/retry. Identical successful request retries replay the original response; changed body under the same epoch/route/request ID => 409 idempotency_conflict. Canonical-body idempotency and answer/state revision publication are atomic. Revocation/retention removes affected experience receipts so old responses cannot revive purged answers. DELETE/reset erases answer state and rotates epoch under the existing boundary.

## Internal observation-backed verification fixtures (not an API)

There is no new public demo route, no --experience-demo flag and no client scenario generator. Internal tests construct recurring_restaurants, mixed_activity, no_flags, insufficient_records and answer_correction by admitting real normalized synthetic observations through the normal deterministic pipeline. They never overwrite or mix actual data or active Plaid connections. The fixture date is server-controlled and provenance remains visibly synthetic. Synthetic preload is not supported. Runtime seeding, fixture observation ingestion and scripted model requests are rejected; legacy demo-source records are excluded from experience without deleting stored history. Plaid Sandbox records retain their explicit Sandbox provenance. Example records and scripted models live only in test targets and are excluded from the runtime image.

## Identity composition boundary

The existing single-instance unauthenticated service is legacy isolated-test mode, not a managed-account implementation. The parent-selected Clerk integration will resolve a verified signed subject plus trusted issuer server-side and compose a separate per-user service/storage/vault/checkpoint/receipt/answer scope. No client user ID is accepted by these request schemas. This change does not guess Clerk settings, implement unverified authentication, or assign an existing anonymous database to the first user. Authenticated isolation is a subsequent parent-owned integration phase; every experience answer in this implementation belongs only to its service instance and epoch.

## Errors, metadata and compatibility

All errors use `$defs.error`, with sanitized message/request_id/details and no source payload echo. Codes add stale_experience and obsolete_pattern only on the new routes; statuses are 400 malformed JSON, 413 body too large, 422 invalid_request/unsupported_version, 404 absent route/not_found, 409 conflicts/consent/busy, and 500 internal_error. Only busy/internal_error are retryable. No answer request sends arbitrary copy, timestamp, raw evidence, provider settings, evaluation date, medical concern or typed free-form context.

The experience implementation is an independently replaceable extension with id=experience/version=1 and async handle(operation,payload)->dict, composed outside core. The existing /health v1 response continues to list only its eight schema-recognized capabilities; /experience/v1 advertises the new version by its route/schema_version. State's shape remains unchanged; experience answers live in private application metadata, never in the public State or model input. Optional synthesis cannot create or override authoritative experience answers or deterministic inference.

The schema includes a complete empty-state example. Mutation examples above are illustrative and contain no usable private identifiers. Frontend handoff: call GET after state mutations or successful reconnect, render typed fields, submit an option using the returned epoch/revision/key, and replace the whole experience with the successful response. On transport/offline/pending changes it may hide stale content or disable controls as a transport-safety measure, but may not synthesize replacement business/health copy.

## Additive inference history

`inference_history` is an oldest-first list (maximum 128) persisted in the same snapshot transaction as the supporting state. Each entry includes `id`, nullable `inferred_at`, `title`, `inference`, `explanation`, `status` (unconfirmed/corrected), `method`, `rule_version`, tap-confirmed `context`, and supporting `evidence` (normalized observation ID/revision, date, source label and concise observed summary). Copy is backend-authored, not private model reasoning. Only actual habit interpretations and corrections enter this history; raw activity, preference cards, empty cards and optional model coverage wording do not.

The current inference method is explicitly deterministic. `inferred_at` is the backend inference-write timestamp, never the response's generated_at, process time or a client clock. The first projection of a legacy stored document has an unknown (null) inference time. Semantic duplicates on refresh/restart retain their original ID and timestamp. Changed evidence/context may produce a new entry. Older responses without this additive field decode as empty history, not fabricated records.

Disabled accounts may retain historical entries but are excluded from current inference eligibility. Erasure/epoch rotation clears history; retention or deletion of supporting evidence removes its duplicate history text. The Ledger displays this saved history, not the current operational card list. No additional HTTP request is required.
