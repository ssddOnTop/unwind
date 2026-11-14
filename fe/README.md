# Local Companion — native iOS 17+

Native SwiftUI Today / Ledger / Settings app, plus a dependency-free Foundation Swift package. All frontend changes are under `fe/`; the plan, research, contracts and backend were read-only to this implementation.

## Verification status (November 14, 2026)

- **32 real Swift Testing tests passed on macOS** using the installed Xcode compiler/frameworks. These are not iOS runtime tests.
- **14 actual Swift-encoded payload cases passed the frozen JSON Schema**, with format validation. Explicit nulls are preserved for nullable contract properties.
- Real sibling-backend **ASGI interoperability** exercised Swift-generated synthetic request templates: explicit demo seed, duplicate ingestion, four feedback variants, complete preferences, deterministic processing, exact deletion retry and old-epoch rejection. This is not URLSession networking or iOS UI execution.
- Complete app + local package: **arm64 iOS 17 device and arm64 iOS 17 simulator typecheck, object compilation and direct Apple linker invocation passed**. Mach-O platform commands are captured separately for both binaries.
- Self-contained `.xcodeproj`, shared app scheme, explicit app source list, Info.plist, HealthKit entitlement, and local Swift package are included. No XcodeGen or network packages are needed. Property lists/project syntax pass `plutil -lint`.
- **Full xcodebuild remains blocked** before building: CoreDevice expects `_XPCTypeBool` from Mercury. No system libraries, global tool selection, license state or signing settings were altered to work around it.
- **No simulator launch, UI automation, real-device run, HealthKit access, OS notification delivery or on-device data-protection test was performed.** Prior research observed an unaccepted-license warning; license state after Xcode's temporary disappearance/reappearance was not revalidated and no boot/install was attempted.

The app is implemented and directly compiles/links, but is **not claimed launch-verified**. Simulator executable is linker-ad-hoc-signed; its Info.plist/resources are not sealed. Device executable is unsigned. The captured `.app` folders/zips are unvalidated build artifacts, not signed distributable apps or validated HealthKit-entitled installations. Use the Xcode project for a properly configured simulator/device build after the environment is repaired by its owner.

During this run `/Applications/Xcode.app` disappeared at approximately 09:00 UTC and reappeared around 09:10 UTC. No partial `.appdownload` contents were used. Diagnostic fallback attempts with the existing Command Line Tools are retained as failures; they do not supersede the successful final Xcode Swift Testing run.

## Run steps

From the workspace root:

```sh
# Actual Foundation Swift Testing tests, isolated caches and logs under fe/evidence.
./fe/scripts/test-core.sh

# Typecheck, emit objects, and directly link the COMPLETE app for both iOS SDKs.
# Also assembles unvalidated .app bundles and ZIP artifacts under fe/evidence.
./fe/scripts/typecheck-ios.sh

# Full Xcode project build probe. Currently exits 255 before the project builds.
python3 fe/scripts/toolchain.py xcode
```

The scripts use `/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin`, explicit SDKROOT/SDK paths, process-local DEVELOPER_DIR, and caches/HOME/TMPDIR under `fe/evidence`. They strip inherited `DYLD_*` values before supplying the research-documented real framework/library paths for macOS Swift Testing. They do not accept licenses, install anything, inject shims, modify system frameworks, boot simulators, or push Git. Python 3 is needed only for build/test tooling, not by the app.

Once a properly licensed working Xcode environment is available:

1. Start the sibling backend using its documented local startup command; ensure it binds **127.0.0.1:8000**, not a public interface. Backend/provider configuration is not managed by this app.
2. Open `fe/LocalCompanion.xcodeproj` in Xcode. Select the shared **LocalCompanion** scheme and an available iOS simulator. Build/run normally. The app scheme is a build/run scheme; its empty UI TestAction is not a test suite. Run core tests with the script above or open the local Core package.
3. The app begins empty. Choose **Refresh / retry saved requests**. Only after explicit confirmation, **Load representative synthetic demo**. It never substitutes fixtures on network failure.
4. Inspect Today and Ledger. The representative demo shows 13,800 recorded steps on three dates, synthetic labels, unknown/partial coverage, evidence revisions, and movement-restriction filtering. These fixture assertions are not clinical validation.
5. In a card, submit correctness separately from usefulness. Event feedback can specify an exact purchase evidence revision and self/shared/other/unknown attribution. **Stop this scenario** immediately pauses local suggestions/reminders and persists suppression on the backend after sync.
6. Settings → **Refresh and edit preferences** persists a full replacement. Old forms are rejected after a revision/epoch change; they are never silently assigned a new request ID and replayed.
7. Optional HealthKit: Settings → **Enable sync and request HealthKit access** is separate explicit consent to normalized local-backend sync and Apple's read permission flow. Then use **Read and sync daily steps now**. No permission or collection is requested at launch.
8. Optional reminders: enable the preference/time and separately request iOS notification permission. Only the backend's eligible, future intents are reconciled. No medical/payment information is placed in notification content.
9. To test offline behavior, stop your test backend, submit feedback, and relaunch the app. The original request ID/body/epoch persist. Retry after bringing the test service back. Do not run deletion/reset experiments against data you intend to retain.
10. **Delete all application data** requires confirmation and remains visibly pending while offline. Successful deletion/reset or a newly observed epoch clears old local cache/outbox and notification requests; deletion never silently reseeds or alters Apple's Health store.

The URL is deliberately fixed to `http://127.0.0.1:8000`, no auth headers. The client rejects alternative hosts/ports and refuses redirects. On a physical iPhone, 127.0.0.1 is the phone, not the Mac: device networking requires a separately approved design. No LAN/public override or credential-entry UI is provided. ATS is local-network-only; actual iOS HTTP access remains a launch-test gate.

## Contract and retry behavior

- Generated Codable types use exact snake_case keys, no implicit key conversion. Decoding rejects missing/unknown properties and invalid enum/version values. Nullable required keys are explicitly encoded as JSON null, not omitted. Dates stay strings.
- The schema is the source of truth. Generation is optional; committed generated models/project are immediately usable:

```sh
python3 fe/scripts/generate-models.py
python3 fe/scripts/generate-project.py
```

- Every mutation is encoded once and durably saved before sending. Retries send the same bytes, request ID and epoch, at most three attempts per sync with bounded backoff. Only transport failures or retryable server errors retry. Permanent rejection persists a blocked marker; relaunch/foreground refresh does not blindly resend it. Explicit discard is required before a replacement edit.
- State is fetched before draining the queue. A different server epoch discards old pending mutations; **nothing retags them**. A same-epoch lower revision, including an idempotency receipt, cannot roll the cached projection backward. A fresh GET follows successful mutations.
- Snapshot/outbox are atomically persisted together. Storage failure/corruption is not treated as an empty queue. Requests older than the 90-day receipt window are discarded with an explicit warning instead of resent.
- Mutations/HealthKit actions are serialized. Preferences require a fresh snapshot and unchanged editor baseline. Pending requests pause suggestion display/reminder scheduling conservatively until reconciled.
- Ledger facts, recurrence thresholds, coverage conclusions, suggestion filtering, cooldowns, synthesis and scheduling decisions remain on the backend. The client only normalizes daily counts, renders returned state, records explicit feedback and reconciles intent identifiers.

## HealthKit boundary and limits

Read-only step type, foreground only, no workouts/location/microphone/Motion permission, no background entitlement, and no HealthKit writes. The adapter requests cumulative statistics for each of seven completed dates in the preference IANA timezone. It uses calendar day boundaries rather than fixed 24-hour increments. It does not separately sum phone/watch samples. Changed aggregates reuse observation identity with the next revision and an exact supersedes reference; timezone changes do not rewrite old records.

Authorization-request completion is **not** read permission. Empty queries and errors report read_access=unknown; only actual returned quantities produce samples_readable. Empty is never converted to zero. Explicit returned zero remains a recorded value, not evidence of inactivity. Queries cannot establish continuous wear/carry coverage; successful observations are partial at most. Protected-data/unavailable/query-failed outcomes contain no raw error or health payload.

Device comparison against Health UI, multi-source reconciliation, limited authorization history, revocation and locked-device behavior remain unverified. Only seven completed days are refreshed; this is not a 90-day rescan or anchored deletion tracker. An empty query cannot distinguish revocation, deletion or genuine absence, so previously uploaded evidence is **not** tombstoned on empty results. Late changes outside the refresh window are not detected by this adapter. Disconnecting immediately excludes the source on the backend but preserves historical evidence until retention or explicit deletion.

## Notifications and privacy

- Generic fixed title/body; no record details in notification payloads. OS permission is checked separately from the backend preference. Notifications are upserted by epoch + backend intent ID and absent/expired intents are removed. Suppression, revocation, pending mutations, offline uncertainty and reset/delete cancel stale reminders.
- iOS can defer/suppress delivery. There is no delivery acknowledgment and no exact daily wakeup promise. Expiry is reconciled on foreground refresh; exact cancellation at midnight while terminated is not guaranteed.
- Live normalized data and the outbox are kept in Application Support with complete file protection on iOS and backup exclusion on both directory and snapshot. No iCloud/CloudKit entitlement or ubiquitous store, no analytics, and no persisted URL cache/cookies.
- Backup exclusion is tested on macOS; iOS locked-device enforcement and backup behavior remain device verification gates. Atomic replacement is not a secure-disk-overwrite guarantee.
- No provider keys/UI. Plaid and email stay visibly not_configured. Cloud consent defaults off; optional live health transmission is not enabled by v1. Scripted synthesis is explicitly labeled and disabled in the UI when live evidence exists.

## Additional reproducible checks

After core tests generate their synthetic encoder output, the already-existing backend virtual environment can validate and exercise it without any installs or writes to backend/contracts. ASGI tests use a temporary database under frontend evidence, no listening port, and synthetic records only:

```sh
PYTHONDONTWRITEBYTECODE=1 be/.venv/bin/python fe/scripts/validate-encoded-contracts.py
PYTHONDONTWRITEBYTECODE=1 HOME="$PWD/fe/evidence/home" XDG_CACHE_HOME="$PWD/fe/evidence/cache" TMPDIR="$PWD/fe/evidence/tmp" \
  be/.venv/bin/python fe/scripts/check-backend-interop.py
python3 fe/scripts/toolchain.py decode-backend
plutil -lint fe/LocalCompanion.xcodeproj/project.pbxproj fe/App/Info.plist fe/App/LocalCompanion.entitlements
```

No backend service was launched or left running by these checks. The ASGI response snapshots are then decoded by the actual Swift Codable types. This is interoperability evidence, **not** proof of app-to-backend networking on iOS.

## Evidence inventory

All generated files/logs are under `fe/evidence`, including:

- `core-tests.json`: exact command/environment, exit status, and real Swift Testing output/count.
- `encoded-contracts.json`, `contract-validation.json`: actual Swift payloads and JSON Schema results.
- `backend-interop.json`, `backend-state-responses.json`, `backend-decoder-run.json`: synthetic real-backend interoperability and Swift decoding results.
- `device-{core,typecheck,objects,link,platform}.json` and corresponding `simulator-*`: exact compiler/linker results and platform inspection.
- `device/LocalCompanion-unvalidated.zip`, `simulator/LocalCompanion-unvalidated.zip`: separately linked unvalidated app bundles; not installed/launched.
- `xcodebuild.json`: full-project loader failure. Diagnostic CLT fallback failures remain labeled separately.
- `verification-summary.json`: concise final gate results.

Remaining acceptance work: repair the Xcode loader using the owner's supported setup process, resolve/verify license status without agent acceptance, build/run through the real project, verify iOS loopback/ATS access, UI flows and accessibility on simulator, and test signing/HealthKit entitlement, actual queries, protection/backup behavior and notification delivery on an explicitly consenting device. No claim of runtime or clinical validation is made.
