# Local Companion — authenticated native iOS 17+

SwiftUI Today / Ledger / Settings app. Clerk owns native authentication; the backend owns Home selection, inference, visit copy, clarification identity and persisted answers. The local Foundation/CryptoKit package has no remote dependencies. The app uses official SwiftPM packages **Clerk 1.7.2** (`ClerkKit`, `ClerkKitUI`) and **Plaid LinkKit 7.4.0**, both exactly pinned with a shared resolved lockfile.

## Banking v1 integration in this branch

This branch includes two reviewable units: the prerequisite frontend authentication/experience/native-infrastructure and frozen-contract/component baseline, then the banking wiring. It does **not** include backend implementation. The authenticated backend must enable **`--banking`** before bank management works; the existing service is not changed or restarted by this work. A missing banking route shows an actionable setup error. There is **no fallback to `/connections/plaid`**, anonymous data or frontend fixtures. Merge requires user approval; nothing is deployed.

### Actual app routing

**Settings → Bank accounts**, and Home's backend-provided connect action, open the actual `BankAccountsView` through `BankingScreen`. The native Link sheet is retained by the account-scoped `BankingController`. The previous simple bank toggle/one-Item controls are no longer mounted. Legacy protocol/controller tests are preserved through an explicitly injected compatibility dependency; the production host never constructs that controller.

The screen displays server institution labels, account names/official names, masks (including leading zeros), type/subtype, availability, import status and server-defined groups. Enable/remove toggles and group actions emit the exact currently rendered opaque IDs and revision. No client grouping or selection policy is added. Removing accounts/groups keeps history, stops future imports and excludes new insights **on the backend**; whole-bank disconnection is separate. New/discovered accounts remain exactly as the server returns them, normally disabled. Update is available per bank and for all manageable banks.

### Wire and native lifecycle

- Only `GET /banking/v1` and `POST /banking/v1/actions` are used. State's schema is unchanged. State and banking revisions must agree before inventory publication. State, experience and notification reconciliation refresh after committed bank changes; stale experience is hidden immediately.
- Every logical action captures one request ID, epoch, expected revision and exact body. Safe transient retries are bounded; stale/obsolete conflicts trigger reads only, never retagging or automatic action resubmission. No optimistic account-selection success is rendered.
- Add Link success sends `exchange` with its opaque session ID and the success public token. Update Link success sends **`reconcile`**, not exchange; callback public token and metadata are ignored for update. Inventory always comes from the server.
- Link tokens, session context, inventory and action requests are memory-only and excluded from JSON cache/outbox/logging. The durable outbox rejects banking actions. An ambiguous one-use exchange is not replayed; the user must inspect refreshed connections before another setup. A proven no-side-effect401 may use the existing single same-session refresh/re-admission retry with identical bytes.
- Link SDK callbacks have their own invalidation ticket. A validated in-flight HTTP result may survive same-account re-admission, but an old SDK flow cannot revive. Cancellation sends `cancel_link` when an admitted current context is available; logout, switch, background/epoch invalidation clear local work and do not send with an obsolete identity. Unconfirmed cancellation is not represented as remote success.
- Replies and current inventory are kept separate. An old receipt cannot restore a removed account or stale selection. Partial outcomes, failed setup and incompatible/missing profiles have explicit status instead of silent empty inventory. No automatic provider sync, model processing or account enabling follows Link success.

### Verification and review scope

The copied baseline passed **129 macOS Swift Testing tests** before its separate commit. The integrated suite currently passes **155**: the original tests, all 24 component handoff tests, 25 new actual host/controller/transport banking regressions and one routing regression. New tests exercise all actions, multi-Item/group identity, explicit null, no optimistic changes, add/update callback separation, stale revisions/epochs, identity switching,401 visibility, cancellation, bounded retries, missing profiles and credential/outbox exclusion. SDK/OS substitutes exist only in the test target. These are not native runtime acceptance results.

Ten actual Swift-encoded action variants (including individual/all-bank sync) and the inventory fixture validate against the frozen JSON Schema with format checks. Debug/Release app builds and UI-test compilation are recorded under ignored `fe/evidence/bank-integration`. No simulator installation, launch, Update tap, provider/model call or backend operation is performed in this worktree.

The obsolete mutating single-bank UI journey is preserved under `Legacy/AnonymousPresentation/SignedInSingleBankAcceptanceTests.swift`, outside targets. Its current replacement is read-only banking navigation for a separately authorized signed-in simulator. Neither it nor the signed-out auth test is run here. Actual add/update Link, live selection/removal, import behavior and multi-Item runtime acceptance await the parent-enabled backend and explicit authorization.

```sh
FE_EVIDENCE_SUBDIR=bank-integration ./fe/scripts/test-core.sh
# Use an existing Python environment with jsonschema; no environment installation is required.
PYTHONDONTWRITEBYTECODE=1 FE_EVIDENCE_SUBDIR=bank-integration \
  "$EXISTING_PYTHON" fe/scripts/validate-banking-contracts.py
python3 fe/scripts/generate-project.py
```

The sections below retain earlier auth/experience verification history. Their single-bank live outcomes are historical and are not evidence for this new banking profile.

## Current profile and boundaries

This supersedes account-free onboarding and client-side Home/visit rules. Missing auth configuration shows a setup screen, never anonymous access. A loaded SDK and active session are necessary but not sufficient: the app also requires a fresh, exact `/auth/v1/session` admission for the configured endpoint, issuer and SDK subject before opening account storage or fetching any data. A legacy backend returning anonymous `/state` cannot unlock the app.

The authenticated API uses **one editable setting**, `APIConfiguration.baseURL` in `fe/Core/Sources/CompanionCore/APIConfiguration.swift`, currently `http://127.0.0.1:8769`. To connect a future deployment, replace that string with its complete origin, e.g. `https://api.example.com`, then rebuild using the concrete-simulator command below (or the normal device/archive workflow when authorized). No project regeneration or auth-config regeneration is needed for an address-only change. No preference, launch argument, environment port or legacy `Local.xcconfig` API-port value overrides it.

Only root origins are supported: HTTPS remotely, HTTP only for `127.0.0.1` or `localhost`; optional valid port and trailing slash are accepted. Paths/prefixes, credentials, queries, fragments and malformed addresses are rejected with a setup message, never silently replaced with localhost. Admission, State, experience and banking share the same captured origin. Redirects remain disabled and Bearer requests are pinned to that origin; the Clerk issuer cannot serve as the API destination. Cache namespaces include the API origin so a deployment change cannot reuse another server's private cache/outbox. Keychain and app identity are unchanged. No endpoint discovery or anonymous fallback is performed.

**One-time server setup is separate:** set container `PUBLIC_API_URL=https://api.example.com` alongside its existing Clerk, Plaid and `DATABASE_URL` configuration. The TLS proxy must preserve that Host and Authorization; forwarded headers are not trusted. The server allows exactly that hostname plus loopback health hosts, never `*`. This does not provision hosting/TLS, change Plaid's `PLAID_WEBHOOK_URL` or register/update existing Item webhooks; those remain explicit deployment work. Changing the frontend address alone cannot configure a server or provider. No deployment or runtime verification is claimed here.

The API-origin slice also updates backend host-boundary configuration; banking business logic and contracts are unchanged. No model/provider keys are collected by the app. No iCloud container or writes are used. No user team ID is fabricated.

## Public Clerk configuration

The project includes `Config/Defaults.xcconfig`, which optionally includes ignored `Config/Local.xcconfig`. Only the two approved **public** settings, `CLERK_PUBLISHABLE_KEY` and `CLERK_ISSUER`, are extracted programmatically from the workspace-root `.env`. The script never emits that file or copies other settings. The exact issuer is encoded for safe xcconfig transport, not normalized or inferred from a token.

From the workspace root:

```sh
python3 fe/scripts/configure-auth.py
python3 fe/scripts/generate-project.py
```

The first command writes only the two public Clerk settings to ignored, mode-0600 `Local.xcconfig`. It does not set an API port/address, and `--api-port` is no longer accepted. Existing public Clerk configuration can be retained unchanged when editing `APIConfiguration.baseURL`. Do not point the API address at an anonymous service or at Clerk; admission failures keep records locked.

For a physical device, choose your actual signing team and HealthKit capability in Xcode. Clerk Native API and the selected sign-in methods must be enabled/configured in the instance dashboard; any production OAuth/native associated-domain configuration remains operator work. The current app successfully displays the real native SDK sign-in form, including its development-mode badge, but that does not establish successful email/OTP or OAuth completion.

## Native sign-in and lifecycle safety

`LocalCompanionApp` mounts the SDK's **AuthView**, not imitation forms. Its completion callback uses the installed SDK's `onAuthComplete` initializer. Tokens come from the captured `Session.getToken(.init(skipCache: ...))`, with no custom JWT template. The SDK manages Keychain credentials; the app never persists or logs JWTs.

- Exactly one Bearer header is injected for each send. Cookies, credential storage, disk HTTP caching and redirects are disabled. Tokens never enter URLs, request bodies or the persistent outbox.
- Admission is a no-cache `GET /auth/v1/session`. It requires HTTP 200, JSON content type, exactly three string fields, schema version 1 and exact issuer/SDK-subject equality. Duplicate keys (including escaped duplicates), extra/missing/null fields, redirects, 304 and legacy 404/405 fail closed. The mismatch message names the API/Clerk configuration, never suggests anonymous access.
- A data 401 invalidates admission, forces SDK refresh at most once for the same captured session, re-admits and retries the same original method/path/body/request ID/epoch. Admission and data share that refresh budget. Concurrent old-generation responses are rejected rather than creating additional refreshes. A second 401, absent/unsafe token or pending session keeps data locked. 503 is retryable service unavailability, not successful sign-in.
- Identity, session, endpoint and auth/admission generations are checked around token fetches, network responses, cache writes and callbacks. A same-user re-admission does not make an old callback current again. A late user-A mutation cannot populate user B's storage, even with matching epochs and request IDs.
- Account namespaces hash `(issuer, subject)`, never raw path components. A deployment change invalidates old server cache. Anonymous snapshots/answers are deleted, never imported. Logout/account switching synchronously gates data, cancels Link/Health/work/reminders and clears the outgoing protected cache/outbox **before** exposing SDK switch/sign-out controls. A failed SDK sign-out leaves a persisted local lock; reopening requires a deliberate user action.
- `UserButton` and `UserProfileView` are exposed only after local clearing. They do not bypass the host's sign-out boundary.
- Backgrounding hides private UI and fences in-flight work while retaining same-account queued bytes on disk. Foreground requires fresh admission before reopening them. Previously authorized generic OS reminders may remain while backgrounded; foreground re-admission, logout/switch and auth failure cancel/reconcile them. Permission prompts only follow explicit user controls.

## Backend-driven experience

The app renders strict typed `GET /experience/v1` responses. POST answers send only the exact `schema_version`, `request_id`, `data_epoch`, `expected_revision`, `clarification_key`, and selected backend option `answer`. No evaluation date, user ID, inference text or client-created answer key is added.

Home displays backend headline/detail/sample label/info and routes the finite action kind. Ledger appears only in its tab. The visit view renders the backend's observed records, possible interpretations, confirmed context, preferences, uncertainties, questions and complete brief. It never computes patterns, category priority, clinical status, thresholds or no-flags copy. Optional expansion and cosmetic editing remain view-local. Copy is explicit, device-local and expires after five minutes; the backend sample marker is preserved after cosmetic editing.

Successful answer responses replace the projection directly after a fresh State check proves they are not older receipts. Lower revisions cannot overwrite newer projections; epoch rotation clears projection/outbox. Stale/obsolete answer conflicts refresh the current question and require a new choice, never automatic retagging/retry. Answers persist only in backend storage, not in the retired local answer store.

Settings retains optional native bank/Health/reminder management, persistent preferences, explicit separate cloud consent, evidence feedback and deletion. Source/permission status is not a clinical inference. Health collects foreground daily cumulative step totals only; successful permission requests and empty queries never claim granted readable access. Local records use file protection and backup exclusion. Provider configuration stays backend-only. Normalized synthetic records may later be preloaded by an authorized backend operator; the frontend neither generates nor seeds them.

## Superseded implementation preserved, not counted as current coverage

`Legacy/AnonymousPresentation/` holds the former `HomePresentation`, `VisitPreparation`, `VisitAnswerStore`, their tests, old onboarding/Home views, anonymous UI tests and anonymous runner. None is in the Swift package or Xcode build. This intentionally retires 56 former local-domain/persistence tests rather than pretending client inference remains authoritative. The original base-contract tests and all 19 Plaid tests remain in the current suite.

Earlier anonymous UI evidence is historical, not authenticated acceptance. In the last pre-auth targeted attempt (`evidence/native-acceptance/Attempt-4.xcresult`), real Link reached Sandbox credentials but the app crashed in an iOS keyboard-accessory logging callback; the visit return interaction also failed. No real Link completion was established. The user superseded that work with this auth migration before another run. All prior failures remain preserved; no anonymous acceptance tests are run by the new project.

## Reproducible builds and tests

A complete owner-configured Xcode and an existing dedicated simulator are required. No script installs software, accepts licenses, changes system libraries or pushes code. The self-contained `.xcodeproj` is generated deterministically with explicit current app/UI test source entries. No XcodeGen is needed.

```sh
FE_EVIDENCE_SUBDIR=auth ./fe/scripts/test-core.sh
FE_EVIDENCE_SUBDIR=auth ./fe/scripts/typecheck-ios.sh
```

Core tests execute actual macOS Swift Testing, not iOS app runtime tests. Scripts strip inherited `DYLD_*`, set explicit SDK/toolchain/framework paths where needed and keep scratch/cache/output under `fe/evidence`. The iOS launcher builds both full device and simulator projects; the device build is unsigned compile/link evidence, not installation.

For normal concrete-simulator Debug and Release builds (no architecture/search-path/signing overrides):

```sh
export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
export DEVICE_ID="YOUR_EXISTING_DEDICATED_SIMULATOR_UDID"
xcodebuild -project fe/LocalCompanion.xcodeproj -scheme LocalCompanion \
  -configuration Debug -destination "platform=iOS Simulator,id=$DEVICE_ID" \
  -derivedDataPath fe/evidence/auth/DebugDerivedData \
  -clonedSourcePackagesDirPath fe/evidence/auth/SourcePackages \
  -packageCachePath fe/evidence/auth/PackageCache build
# Repeat with Release and fe/evidence/auth/ReleaseDerivedData.
```

**Do not run the following signed-out UI test on the newly signed-in user’s current simulator.** It terminates/relaunches the test app. Preserve that session; use a separately authorized signed-out simulator for any future rerun. The already-passing native **signed-out sign-in-screen** test is reproducible as follows:

```sh
xcodebuild -project fe/LocalCompanion.xcodeproj -scheme LocalCompanion \
  -configuration Debug -destination "platform=iOS Simulator,id=$DEVICE_ID" \
  -derivedDataPath fe/evidence/auth/DebugDerivedData \
  -clonedSourcePackagesDirPath fe/evidence/auth/SourcePackages \
  -packageCachePath fe/evidence/auth/PackageCache \
  -parallel-testing-enabled NO -resultBundlePath fe/evidence/auth/NewAuthScreen.xcresult \
  -only-testing:LocalCompanionUITests/AuthRuntimeTests/testNativeClerkSignInAndNoAnonymousTabs test
```

Use a new result name; prior results are never overwritten. The test waits for the actual Clerk identifier/Continue controls, verifies anonymous tabs/setup/seed controls are absent and checks the gate after relaunch. It does not type an identifier, request an OTP, create an account, synthesize a token or access any companion backend. It refuses its screenshot if an existing identifier is present.

Actual Swift-encoded experience payloads can be validated with an existing Python environment containing `jsonschema` (no installs required):

```sh
PYTHONDONTWRITEBYTECODE=1 FE_EVIDENCE_SUBDIR=auth \
  be/.venv/bin/python fe/scripts/validate-experience-contracts.py
```

## Offline manual-update feedback regression

The earlier silent-update incident is **not retrospectively attributed to a401**. An offline regression did reproduce that precise failure path in the actual `PlaidController` and `AppModel` sources: ordinary sync passed, while a401/forced-refresh/re-admission/exact-byte retry returned a valid result but left `lastSync` nil. The original admission callback rotated the shared ticket, so the controller rejected the permitted operation's response along with stale SDK callbacks.

The fix separates native Link callback invalidation from the in-flight HTTP operation. Only a request accepted by the existing identity/session/epoch/transport checks can publish its result after re-admission. Recovered waiting/partial results stay pending and do not start another automatic poll. Cancelled/interrupted operations have visible notices; error outcomes finish without false success or stuck working. Logout, account switch and epoch changes still discard delayed results. Old native Link callbacks cannot reopen a sheet or erase a completed result.

**105 actual macOS Swift Testing tests pass**, including 15 new controller-flow regressions. Relative symlinks in the test target compile the actual app/controller implementations; only SDK/Health/notification dependencies are test doubles, and those are excluded from the app bundle. This is not an iOS runtime or live provider test. The before-fix failure and final passes are preserved under `fe/evidence/offline-sync`; `verification-summary.json` records scope. Build-only Debug verification against the simulator SDK passed using cached pinned dependencies with package updates disabled. Nothing was installed/launched, and no live account, Item, simulator state or backend was changed in this slice.

## Latest signed-in acceptance on the user's iPhone 16

The actual existing signed-in app on the user-designated iPhone 16 (not the earlier UI-smoke simulator) passed Home/Settings/Ledger inspection against authenticated port 8769. The UI showed backend-provided facts: one restaurant-category purchase, one merchant and one purchase date; no activity records and no eligible suggestion under the current preferences. Synthetic Sandbox provenance and uncertainty remained explicit. No frontend seed or local inference was used.

Settings already displayed **Bank: Connected · Sandbox**. Therefore no Link flow or second Item was created. Exactly one **Update bank records** tap was attempted. No server-confirmed result appeared within 45 seconds; the last captured UI still displayed Connected/Sandbox without a visible bank error. Sync success/import readiness is **unverified**, not complete. The manual update was not repeated. The initial native Link flow that established the existing connection was not observed or proved by this pass.

Evidence is private/ignored under `fe/evidence/signed-in-acceptance`: corrected inspection passed; one Settings-selector failure and the update-result timeout are preserved. All 90 macOS core tests passed again. `handoff-summary.json` contains sanitized status/counts and result timings. Account/session and existing connection are preserved; no logout/reset/uninstall/disconnect/delete, backend edit/restart or model/provider-processing action was performed. Parent requested this focused handoff with no additional update attempts. Future multi-bank/account-selection/removal semantics await a frozen backend contract; no guessed API was added.

## Evidence and remaining acceptance gates

Current verification: **90 actual macOS Swift Testing tests pass** (36 base/compatibility, 19 Plaid, 22 auth, 10 experience, 3 app-wiring); **1 signed-out native Clerk UI test passed before the user signup update**. Final concrete Debug/Release simulator and unsigned device builds pass. All 25 actual encoded schema cases pass (14 base, 9 Plaid, 2 experience). Fixture resources are absent from all three app bundles. No signed-out UI tests or app lifecycle manipulation were repeated after the parent asked to preserve the new account.

Ignored `fe/evidence/auth/` contains core output, concrete Debug/Release and unsigned device build commands/results, exact SDK release/API inspection, schema validation, failed initial UI compile evidence, passing native-screen XCTest bundles and a reviewed screenshot. `integration-summary.json` records final measured counts and build statuses.

The real Clerk sign-in screen passed before the parent reported that the user had just created an account. That account/session is now preserved: no signed-out UI rerun, restart, installation, logout, deletion, bank connection or provider call is performed after that update. The last private screenshot showed the simulator home screen, so it does not establish a signed-in app session or live backend admission. Port 8769 is the confirmed authenticated backend. The parent will coordinate actual signed-in data/Plaid acceptance; no account, credential or token was fabricated.

Consequently live token verification, real two-user server isolation, authenticated experience answer persistence, SDK account-switch/logout behavior, authenticated native Plaid completion and physical-device Health/notification/protection behavior are not claimed as end-to-end verified. Mocks, successful compilation and a native sign-in-screen pass do not prove those flows.
